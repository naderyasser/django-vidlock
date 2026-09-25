"""Spotting download tools by how they ask for keys.

Two patterns give a tool away:

* **Depth** — the same video's key again and again. hls.js fetches a key once
  per page load and caches it; a tool fetches it once per run, and someone
  retrying or scripting runs it many times.
* **Breadth** — many different videos' keys in a short time. Harvesting a
  whole course fetches each lesson's key exactly once, which the depth limit
  never sees, while a person watches a few lessons an hour.

Counts live in Django's cache per viewer and clock hour. A cache that is down
fails open, because a counter must never cost a paying viewer their video.
"""

from __future__ import annotations

from django.core.cache import cache
from django.utils import timezone

from vidlock import conf

_PREFIX = 'vidlock'

#: Reasons passed to ``vidlock.signals.key_abuse``.
DEPTH, BREADTH = 'depth', 'breadth'


def _hour() -> str:
    return timezone.now().strftime('%Y%m%d%H')


def _count(key: str) -> int:
    cache.add(key, 0, 3600)
    return cache.incr(key)


def key_fetch_allowed(user_id, video_id, namespace: str = '') -> bool:
    """Count one key fetch; False once the hourly limit for this video is exceeded.

    ``namespace`` keeps counters apart when one cache serves several sites or
    tenants that can reuse ids.
    """
    return check_key_fetch(user_id, video_id, namespace) is None


def check_key_fetch(user_id, video_id, namespace: str = '') -> str | None:
    """Count one key fetch. None when it is allowed, else the limit it broke
    (``DEPTH`` or ``BREADTH``)."""
    hour = _hour()
    try:
        fetches = _count(f'{_PREFIX}:fetch:{namespace}:{user_id}:{video_id}:{hour}')
        if fetches > int(conf.get('KEY_FETCHES_PER_HOUR')):
            return DEPTH
        breadth = conf.get('KEY_VIDEOS_PER_HOUR')
        if breadth and fetches == 1:
            # The first fetch of this video this hour widens the viewer's reach.
            videos = _count(f'{_PREFIX}:videos:{namespace}:{user_id}:{hour}')
            if videos > int(breadth):
                return BREADTH
        elif breadth:
            videos = cache.get(f'{_PREFIX}:videos:{namespace}:{user_id}:{hour}') or 0
            if videos > int(breadth):
                return BREADTH
    except Exception:
        return None
    return None


def first_report_today(user_id, video_id, namespace: str = '', reason: str = DEPTH) -> bool:
    """True once per viewer, video (or, for breadth, viewer) and day — so an
    alert is not a flood."""
    subject = '*' if reason == BREADTH else video_id
    try:
        return cache.add(f'{_PREFIX}:reported:{reason}:{namespace}:{user_id}:{subject}', 1, 24 * 3600)
    except Exception:
        return False
