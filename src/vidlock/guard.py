"""Spotting download tools by how often they ask for the key.

hls.js fetches a video's key once per page load and caches it by URI. A
download tool fetches it once per run, and someone harvesting a course runs
it again and again. The count is kept in Django's cache per viewer, video
and clock hour; a cache that is down fails open, because a counter must
never cost a paying viewer their video.
"""

from django.core.cache import cache
from django.utils import timezone

from vidlock import conf

_PREFIX = 'vidlock'


def key_fetch_allowed(user_id, video_id, namespace=''):
    """Count one key fetch; False once the hourly limit is exceeded.

    ``namespace`` keeps counters apart when one cache serves several sites or
    tenants that can reuse ids.
    """
    limit = int(conf.get('KEY_FETCHES_PER_HOUR'))
    bucket = timezone.now().strftime('%Y%m%d%H')
    key = f'{_PREFIX}:fetch:{namespace}:{user_id}:{video_id}:{bucket}'
    try:
        cache.add(key, 0, 3600)
        return cache.incr(key) <= limit
    except Exception:  # noqa: BLE001
        return True


def first_report_today(user_id, video_id, namespace=''):
    """True once per viewer, video and day — so an alert is not a flood."""
    try:
        return cache.add(f'{_PREFIX}:reported:{namespace}:{user_id}:{video_id}', 1, 24 * 3600)
    except Exception:  # noqa: BLE001
        return False
