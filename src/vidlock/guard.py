"""Spotting download tools by how they ask for keys.

Three patterns give a tool away:

* **Depth** — the same key again and again. hls.js fetches a key once per
  page load and caches it; a tool fetches it once per run, and someone
  retrying or scripting runs it many times.
* **Breadth** — many different videos' keys in a short time. Harvesting a
  whole course fetches each lesson's keys once, which the depth limit never
  sees, while a person watches a few lessons an hour.
* **Pace** — a rotated video's keys faster than anyone could watch. A viewer
  needs the next key every ``KEY_ROTATION_SECONDS``; a tool wants all of them
  now. Keys come out of a bucket that holds ``KEY_BURST`` and refills at
  ``KEY_PACE`` times playback speed, per viewer and video (a new token does
  not refill it). Keys already given to that viewer are free to fetch again.

Counts live in Django's cache. A cache that is down fails open, because a
counter must never cost a paying viewer their video.
"""

from __future__ import annotations

import time

from django.core.cache import cache
from django.utils import timezone

from vidlock import conf

_PREFIX = 'vidlock'

#: Reasons passed to ``vidlock.signals.key_abuse`` and to ``vidlock.risk``.
DEPTH, BREADTH, PACE = 'depth', 'breadth', 'pace'


def _hour() -> str:
    return timezone.now().strftime('%Y%m%d%H')


def _count(key: str, ttl: int = 3600) -> int:
    cache.add(key, 0, ttl)
    return cache.incr(key)


def key_fetch_allowed(user_id, video_id, namespace: str = '') -> bool:
    """Count one key fetch; False once a limit is exceeded.

    ``namespace`` keeps counters apart when one cache serves several sites or
    tenants that can reuse ids.
    """
    return check_key_fetch(user_id, video_id, namespace) is None


def check_key_fetch(
    user_id, video_id, namespace: str = '', index: int = 0, key_seconds: float | None = None
) -> str | None:
    """Count one fetch of key ``index``. None when it is allowed, else the
    limit it broke (``DEPTH``, ``BREADTH`` or ``PACE``). ``key_seconds`` is how
    much video one key covers; pass it for a rotated video to pace its keys."""
    hour = _hour()
    try:
        fetches = _count(f'{_PREFIX}:fetch:{namespace}:{user_id}:{video_id}:{index}:{hour}')
        if fetches > int(conf.get('KEY_FETCHES_PER_HOUR')):
            return DEPTH

        breadth = conf.get('KEY_VIDEOS_PER_HOUR')
        if breadth:
            videos_key = f'{_PREFIX}:videos:{namespace}:{user_id}:{hour}'
            # The first key of this video this hour widens the viewer's reach.
            if cache.add(f'{_PREFIX}:seen:{namespace}:{user_id}:{video_id}:{hour}', 1, 3600):
                videos = _count(videos_key)
            else:
                videos = cache.get(videos_key) or 0
            if videos > int(breadth):
                return BREADTH

        if key_seconds and not _paced(user_id, video_id, namespace, index, key_seconds):
            return PACE
    except Exception:
        return None
    return None


def _paced(user_id, video_id, namespace, index, key_seconds) -> bool:
    burst = float(conf.get('KEY_BURST'))
    speed = float(conf.get('KEY_PACE') or 0)
    if speed <= 0:
        return True
    key = f'{_PREFIX}:pace:{namespace}:{user_id}:{video_id}'
    now = time.time()
    state = cache.get(key) or {'issued': [], 'tokens': burst, 'at': now}
    if index in state['issued']:
        return True
    interval = max(1.0, float(key_seconds) / speed)
    tokens = min(burst, state['tokens'] + (now - state['at']) / interval)
    if tokens < 1:
        cache.set(key, {**state, 'tokens': tokens, 'at': now}, 6 * 3600)
        return False
    state = {'issued': [*state['issued'], index][-500:], 'tokens': tokens - 1, 'at': now}
    cache.set(key, state, 6 * 3600)
    return True


def retry_after(key_seconds: float | None) -> int:
    """Seconds until a paced viewer earns the next key."""
    speed = float(conf.get('KEY_PACE') or 0)
    if not key_seconds or speed <= 0:
        return 60
    return max(1, int(float(key_seconds) / speed))


def first_report_today(user_id, video_id, namespace: str = '', reason: str = DEPTH) -> bool:
    """True once per viewer, video (or, for breadth, viewer) and day — so an
    alert is not a flood."""
    subject = '*' if reason == BREADTH else video_id
    try:
        return cache.add(f'{_PREFIX}:reported:{reason}:{namespace}:{user_id}:{subject}', 1, 24 * 3600)
    except Exception:
        return False
