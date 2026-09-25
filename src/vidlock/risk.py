"""A running suspicion score per viewer and day, and what to do past it.

No single signal proves piracy — a viewer on a train changes IPs, a curious
student opens DevTools. Together they tell. Every event below adds its weight
(``RISK_WEIGHTS``) to the viewer's score for the day; the first time the score
reaches ``RISK_THRESHOLD`` vidlock logs a warning and sends
``vidlock.signals.viewer_flagged``; with ``RISK_SUSPEND_SECONDS`` it also
refuses that viewer every playlist, key and heartbeat for that long.

Events:

``depth``, ``breadth``, ``pace``
    A key limit in ``vidlock.guard`` refused a key.
``fetch_metadata``
    A web key request that came cross-site, as a page load, or without the
    Sec-Fetch-* headers in strict mode.
``raw_key``
    A web key fetched without the bundled player's key exchange — what a
    download tool fed a copied cookie does (and very old iPhones).
``key_without_playback``
    A key handed to the browser player that never reported playing.
``takeover``
    A device took the stream from another under ``MAX_STREAMS``.
``many_ips``
    One more network than ``MAX_NETWORKS_PER_DAY`` for the viewer today.
``tamper``
    The player found MediaSource functions replaced by a page script or an
    extension — how "save the stream" extensions work.

Kept in Django's cache for a day. To keep a record, listen to the signal.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import time

from django.core.cache import cache
from django.utils import timezone

from vidlock import conf, signals

logger = logging.getLogger(__name__)

DEFAULT_WEIGHTS = {
    'depth': 3,
    'breadth': 5,
    'pace': 3,
    'fetch_metadata': 2,
    'raw_key': 1,
    'key_without_playback': 3,
    'takeover': 2,
    'many_ips': 1,
    'tamper': 4,
}

_PREFIX = 'vidlock:risk'
#: How long a key may wait for its first heartbeat.
PLAYBACK_GRACE = 120


def _day() -> str:
    return timezone.now().strftime('%Y%m%d')


def weights() -> dict:
    return {**DEFAULT_WEIGHTS, **(conf.get('RISK_WEIGHTS') or {})}


def note(user, event: str, namespace: str = '', request=None, once: str = '') -> int:
    """Add ``event`` to the viewer's score; returns the score. ``once`` makes
    the event count at most once per that key and day."""
    weight = float(weights().get(event, 1))
    if weight <= 0:
        return 0
    user_id = getattr(user, 'pk', user)
    day = _day()
    try:
        if once and not cache.add(f'{_PREFIX}:once:{namespace}:{user_id}:{event}:{once}:{day}', 1, 86400):
            return score(user_id, namespace)
        key = f'{_PREFIX}:score:{namespace}:{user_id}:{day}'
        state = cache.get(key) or {'score': 0.0, 'events': {}}
        state['score'] += weight
        state['events'][event] = state['events'].get(event, 0) + 1
        cache.set(key, state, 86400)
        if state['score'] >= float(conf.get('RISK_THRESHOLD')) and cache.add(
            f'{_PREFIX}:flagged:{namespace}:{user_id}:{day}', 1, 86400
        ):
            _flag(user, state, namespace, request)
        return state['score']
    except Exception:
        return 0


def _flag(user, state, namespace, request):
    logger.warning(
        'vidlock: viewer %s flagged, score %s: %s', getattr(user, 'pk', user), state['score'], state['events']
    )
    seconds = int(conf.get('RISK_SUSPEND_SECONDS') or 0)
    if seconds:
        cache.set(_suspended_key(getattr(user, 'pk', user), namespace), dict(state['events']), seconds)
    signals.viewer_flagged.send(
        sender=None,
        user=user,
        score=state['score'],
        events=dict(state['events']),
        suspended_for=seconds,
        request=request,
    )


def score(user_id, namespace: str = '') -> float:
    try:
        return (cache.get(f'{_PREFIX}:score:{namespace}:{user_id}:{_day()}') or {}).get('score', 0.0)
    except Exception:
        return 0.0


def _suspended_key(user_id, namespace):
    return f'{_PREFIX}:suspended:{namespace}:{user_id}'


def is_suspended(user_id, namespace: str = '') -> bool:
    try:
        return cache.get(_suspended_key(user_id, namespace)) is not None
    except Exception:
        return False


def clear(user_id, namespace: str = '') -> None:
    """Lift a suspension and reset today's score (``manage.py vidlock_risk --clear``)."""
    with contextlib.suppress(Exception):
        cache.delete_many(
            [
                _suspended_key(user_id, namespace),
                f'{_PREFIX}:score:{namespace}:{user_id}:{_day()}',
                f'{_PREFIX}:flagged:{namespace}:{user_id}:{_day()}',
            ]
        )


# -- networks -----------------------------------------------------------------


def network(ip: str) -> str:
    """The /24 (IPv4) or /48 (IPv6) an address belongs to: a phone hopping
    between addresses of one carrier stays on one network."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ''
    prefix = 24 if address.version == 4 else 48
    return str(ipaddress.ip_network(f'{address}/{prefix}', strict=False))


def seen_from(user, ip: str, namespace: str = '', request=None) -> None:
    limit = conf.get('MAX_NETWORKS_PER_DAY')
    net = network(ip)
    if not limit or not net:
        return
    user_id = getattr(user, 'pk', user)
    key = f'{_PREFIX}:nets:{namespace}:{user_id}:{_day()}'
    with contextlib.suppress(Exception):
        nets = cache.get(key) or []
        if net in nets:
            return
        nets.append(net)
        cache.set(key, nets[-100:], 86400)
        if len(nets) > int(limit):
            note(user, 'many_ips', namespace, request)


# -- keys that are never played ----------------------------------------------


def _pending_key(user_id, namespace):
    return f'{_PREFIX}:pending:{namespace}:{user_id}'


def keyed(user_id, lease: str, video_id, namespace: str = '') -> None:
    """The browser player got a key: expect its heartbeat soon."""
    with contextlib.suppress(Exception):
        pending = cache.get(_pending_key(user_id, namespace)) or {}
        pending.setdefault(f'{lease}|{video_id}', time.time())
        cache.set(_pending_key(user_id, namespace), pending, 86400)


def played(user_id, lease: str, video_id, namespace: str = '') -> None:
    with contextlib.suppress(Exception):
        pending = cache.get(_pending_key(user_id, namespace)) or {}
        if pending.pop(f'{lease}|{video_id}', None) is not None:
            cache.set(_pending_key(user_id, namespace), pending, 86400)


def sweep(user, namespace: str = '', request=None) -> None:
    """Count the keys that waited past the grace period for a heartbeat."""
    user_id = getattr(user, 'pk', user)
    try:
        pending = cache.get(_pending_key(user_id, namespace)) or {}
        now = time.time()
        stale = [entry for entry, at in pending.items() if now - at > PLAYBACK_GRACE]
        if not stale:
            return
        for entry in stale:
            del pending[entry]
        cache.set(_pending_key(user_id, namespace), pending, 86400)
    except Exception:
        return
    for entry in stale:
        note(user, 'key_without_playback', namespace, request, once=entry)
