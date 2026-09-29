"""Who watched what: progress from the player's heartbeat.

The bundled player notes the stretches of video it actually played
(``[start, end]`` in seconds, split at every seek or pause) and sends them
with each heartbeat. Here they become, per viewer and video, seconds watched
and a count per ``WatchProgress.BUCKET_SECONDS`` of video — so a teacher can
see who really watched a lesson, where they stopped, and which part everyone
replays.

The stretches come from the viewer's browser, so a heartbeat may claim no
more video than could have played since the one before (at up to 2.5x);
anything beyond is dropped. It is analytics, not proof.

    from vidlock import progress

    progress.report(lesson)        # the class: viewers, completion, heatmap
    progress.for_viewer(user)      # one student's progress across videos
"""

from __future__ import annotations

import math
import time

from django.core.cache import cache
from django.db import transaction

from vidlock import conf

MAX_SPEED = 2.5
#: A seconds allowance per heartbeat on top of the elapsed time (clock skew,
#: the first heartbeat of a page).
SLACK = 45
#: A bucket counts as watched when one stretch covers this much of it, so a
#: second of skimming past does not.
MIN_OVERLAP = 3.0


def _label(video) -> str:
    return video._meta.label


def _clean(watched) -> list[tuple[float, float]]:
    stretches = []
    for item in watched if isinstance(watched, list) else []:
        try:
            start, end = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isfinite(start) and math.isfinite(end) and 0 <= start < end:
            stretches.append((start, end))
    return stretches[:200]


def _budget(user_id, video_key: str) -> float:
    """Seconds of video this heartbeat may add: what could have played since
    the last one."""
    key = f'vidlock:progress:last:{user_id}:{video_key}'
    now = time.time()
    try:
        last = cache.get(key)
        cache.set(key, now, 6 * 3600)
    except Exception:
        last = None
    elapsed = now - last if last else 0
    return min(elapsed, 3600) * MAX_SPEED + SLACK


def record(user, video, watched, position=None) -> None:
    """Add the stretches a heartbeat reported to the viewer's progress."""
    if not conf.get('TRACK_PROGRESS'):
        return
    from vidlock.models import WatchProgress

    label, video_id = _label(video), str(video.pk)
    duration = float(getattr(video, 'sealed_duration', 0) or 0)
    budget = _budget(user.pk, f'{label}:{video_id}')
    kept, total = [], 0.0
    for start, stop in _clean(watched):
        if duration:
            stop = min(stop, duration)  # noqa: PLW2901 — clipped to the video
        if total >= budget or stop <= start:
            continue
        stop = min(stop, start + budget - total)  # noqa: PLW2901 — clipped to the budget
        kept.append((start, stop))
        total += stop - start
    if not kept:
        return
    size = WatchProgress.BUCKET_SECONDS
    with transaction.atomic():
        row, _ = WatchProgress.objects.select_for_update().get_or_create(
            user=user, video_type=label, video_id=video_id
        )
        buckets = list(row.buckets or [])
        for start, stop in kept:
            first, last = int(start // size), int(max(start, stop - 1e-6) // size)
            for index in range(first, last + 1):
                overlap = min(stop, (index + 1) * size) - max(start, index * size)
                # The video's last bucket may be shorter than MIN_OVERLAP.
                length = min(size, duration - index * size) if duration else size
                if overlap >= min(MIN_OVERLAP, length) - 1e-6:
                    if len(buckets) <= index:
                        buckets.extend([0] * (index + 1 - len(buckets)))
                    buckets[index] += 1
        row.buckets = buckets
        row.seconds_watched += total
        if duration:
            row.duration = duration
        if isinstance(position, (int, float)) and math.isfinite(position) and position >= 0:
            row.position = float(position)
        row.save()


def report(video, complete_at: float = 0.9) -> dict:
    """The class's view of one video: how many watched, how far, and where."""
    from vidlock.models import WatchProgress

    rows = list(WatchProgress.objects.filter(video_type=_label(video), video_id=str(video.pk)))
    duration = float(getattr(video, 'sealed_duration', 0) or 0) or max((r.duration for r in rows), default=0)
    size = WatchProgress.BUCKET_SECONDS
    length = math.ceil(duration / size) if duration else max((len(r.buckets) for r in rows), default=0)
    heatmap = [0] * length
    for row in rows:
        for index, count in enumerate(row.buckets[:length]):
            heatmap[index] += count
    completions = [row.completion for row in rows]
    return {
        'viewers': len(rows),
        'completed': sum(1 for c in completions if c >= complete_at),
        'average_completion': round(sum(completions) / len(completions), 3) if completions else 0.0,
        'seconds_watched': round(sum(row.seconds_watched for row in rows), 1),
        'hours_watched': round(sum(row.seconds_watched for row in rows) / 3600, 2),
        'bucket_seconds': size,
        'heatmap': heatmap,
        'most_replayed_at': heatmap.index(max(heatmap)) * size if any(heatmap) else None,
    }


def for_viewer(user):
    """One viewer's progress rows, most recent first."""
    from vidlock.models import WatchProgress

    return WatchProgress.objects.filter(user=user).order_by('-last_seen')
