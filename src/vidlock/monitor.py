"""Monitor mode: what vidlock would refuse, without refusing it.

With ``VIDLOCK['ENFORCE'] = False`` every protective rule still runs — the
limits count, the risk score adds up — but instead of a 403/409/429 the
request goes through, and the would-be refusal is counted here, logged and
sent as ``vidlock.signals.monitored``. Run a week like that on real viewers,
look at ``manage.py vidlock_status``, tune, then enforce.

Tokens, sessions, password changes and ``can_watch()`` are never in monitor
mode: those are access control, not heuristics.
"""

from __future__ import annotations

import contextlib
import logging

from django.core.cache import cache
from django.utils import timezone

from vidlock import conf, signals

logger = logging.getLogger(__name__)

#: The rules monitor mode can hold back.
RULES = ('pace', 'depth', 'breadth', 'fetch_metadata', 'raw_key', 'elsewhere', 'suspended', 'suspend')


def _key(day=None) -> str:
    return f'vidlock:monitor:{(day or timezone.localdate()).isoformat()}'


def would_refuse(rule: str, request=None, user=None, video=None) -> bool:
    """True when ``rule`` should refuse now; in monitor mode, count it and
    return False so the request goes through."""
    if conf.enforcing():
        return True
    with contextlib.suppress(Exception):
        counts = cache.get(_key()) or {}
        counts[rule] = counts.get(rule, 0) + 1
        cache.set(_key(), counts, 8 * 24 * 3600)
    logger.info('vidlock monitor: would refuse %s for user %s', rule, getattr(user, 'pk', user))
    signals.monitored.send(sender=None, rule=rule, request=request, user=user, video=video)
    return False


def counts(day=None) -> dict:
    """``{rule: how many times it would have refused}`` for a day (default today)."""
    try:
        return dict(cache.get(_key(day)) or {})
    except Exception:
        return {}
