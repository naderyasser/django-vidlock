"""One playback at a time: leases kept alive by the player's heartbeat.

With ``MAX_STREAMS`` set, each viewer holds at most that many leases. A
lease belongs to a browser session (or an app login), so two tabs of one
browser share one, while a friend's laptop with the same password takes a
lease of its own — and, past the limit, takes it from the oldest. The key
and playlist views only serve a token whose lease is still held, and the
player stops with a message when its heartbeat learns it lost the lease.

A lease nobody heartbeats for ``STREAM_TIMEOUT`` seconds lapses, and comes
back by itself unless another device took its place meanwhile. Held in
Django's cache: use a shared one (Redis, Memcached, database) in production.
"""

from __future__ import annotations

import contextlib
import secrets
import time

from django.core.cache import cache

from vidlock import conf, tokens

_PREFIX = 'vidlock:streams'


def _key(user_id, namespace: str) -> str:
    return f'{_PREFIX}:{namespace}:{user_id}'


def _limit() -> int:
    return int(conf.get('MAX_STREAMS') or 0)


def _live(leases: dict, now: float) -> dict:
    timeout = float(conf.get('STREAM_TIMEOUT'))
    return {lease: seen for lease, seen in leases.items() if now - seen <= timeout}


def lease_for(request, channel: str) -> str:
    """A stable id for the device making ``request``: its session on the web,
    its Authorization header in an app, else a fresh one."""
    if channel == tokens.WEB:
        found = tokens.session_fingerprint(request)
        if found:
            return found
    auth = request.headers.get('Authorization', '')
    if auth:
        return tokens._fingerprint('lease', auth)
    return secrets.token_hex(8)


HELD, TOOK, REFUSED = 'held', 'took', 'refused'


def open_lease(user_id, lease: str, namespace: str = '', take: bool = True) -> str:
    """Hold ``lease`` for the viewer. ``TOOK`` when that took another device's
    lease away (the viewer went past ``MAX_STREAMS``); ``REFUSED`` when it
    would have, but ``take`` is False — the player's own automatic reloads
    never take a stream back, so two devices cannot keep stealing it."""
    limit = _limit()
    if not limit:
        return HELD
    now = time.time()
    try:
        leases = _live(cache.get(_key(user_id, namespace)) or {}, now)
        outcome = HELD
        if lease not in leases:
            if len(leases) >= limit and not take:
                return REFUSED
            while len(leases) >= limit:
                oldest = min(leases, key=leases.get)
                del leases[oldest]
                outcome = TOOK
        leases[lease] = now
        cache.set(_key(user_id, namespace), leases, 24 * 3600)
        return outcome
    except Exception:
        return HELD


def _keep(user_id, lease: str, namespace: str) -> bool:
    """Refresh ``lease``; a lapsed one (a laptop asleep, a lost connection)
    comes back while no other device has taken its place."""
    now = time.time()
    leases = _live(cache.get(_key(user_id, namespace)) or {}, now)
    if lease not in leases and len(leases) >= _limit():
        return False
    leases[lease] = now
    cache.set(_key(user_id, namespace), leases, 24 * 3600)
    return True


def holds(user_id, lease: str, namespace: str = '') -> bool:
    """Whether ``lease`` may still play. Always True without ``MAX_STREAMS``."""
    if not _limit():
        return True
    try:
        return _keep(user_id, lease, namespace)
    except Exception:
        return True


def touch(user_id, lease: str, namespace: str = '') -> bool:
    """A heartbeat: keep ``lease`` alive. False when it was taken over."""
    return holds(user_id, lease, namespace)


def release_all(user_id, namespace: str = '') -> None:
    """Drop every lease the viewer holds (e.g. after a password reset)."""
    with contextlib.suppress(Exception):
        cache.delete(_key(user_id, namespace))
