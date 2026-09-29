"""Who watched what: progress from the player's heartbeat.

The bundled player notes the stretches of video it actually played
(``[start, end]`` in seconds, split at every seek or pause) and sends them
with each heartbeat. Here they become, per viewer and video, seconds watched
and a count per ``WatchProgress.BUCKET_SECONDS`` of video — so a teacher can
see who really watched a lesson, where they stopped, and which part everyone
replays.

The stretches come from the viewer's browser, so what a viewer may claim is
an allowance that fills at 2.5x real time and that every heartbeat spends:
claims beyond it are dropped, however many heartbeats carry them. It is
analytics, not proof.

    from vidlock import progress

    progress.report(lesson)        # the class: viewers, completion, heatmap
    progress.for_viewer(user)      # one student's progress across videos
"""

from __future__ import annotations

import contextlib
import math
import time

from django.core.cache import cache
from django.db import transaction

from vidlock import conf

MAX_SPEED = 2.5
#: What a viewer's first heartbeat may claim (it cannot know the page's age).
SLACK = 45
#: The most a viewer can bank by not sending heartbeats (a tab in the
#: background, a lost connection): ten minutes of video.
ALLOWANCE_CAP = 600
#: Seconds between one reported piece's end and the next one's start that
#: still make them one continuous play.
JOIN_GAP = 1.5
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


def _budget(user_id, video_key: str, namespace: str = '') -> float:
    """Seconds of video this heartbeat may add: an allowance that fills at
    MAX_SPEED times real time (up to ALLOWANCE_CAP) and that each heartbeat
    spends. Back-to-back heartbeats get nothing new."""
    key = f'vidlock:progress:allow:{namespace}:{user_id}:{video_key}'
    now = time.time()
    try:
        state = cache.get(key)
    except Exception:
        state = None
    if state is None:
        return SLACK
    return min(ALLOWANCE_CAP, state['left'] + (now - state['at']) * MAX_SPEED)


def _spend(user_id, video_key: str, left: float, namespace: str = '') -> None:
    with contextlib.suppress(Exception):
        cache.set(
            f'vidlock:progress:allow:{namespace}:{user_id}:{video_key}',
            {'left': max(0.0, left), 'at': time.time()},
            6 * 3600,
        )


def record(user, video, watched, position=None, namespace: str = '') -> None:
    """Add the stretches a heartbeat reported to the viewer's progress."""
    if not conf.get('TRACK_PROGRESS'):
        return
    from vidlock.models import WatchProgress

    label, video_id = _label(video), str(video.pk)
    duration = float(getattr(video, 'sealed_duration', 0) or 0)
    if duration <= 0:
        return  # nothing sealed to measure against
    budget = _budget(user.pk, f'{label}:{video_id}', namespace)
    kept, total = [], 0.0
    for start, stop in _clean(watched):
        stop = min(stop, duration)  # noqa: PLW2901 — clipped to the video
        if total >= budget or stop <= start:
            continue
        stop = min(stop, start + budget - total)  # noqa: PLW2901 — clipped to the budget
        kept.append((start, stop))
        total += stop - start
    _spend(user.pk, f'{label}:{video_id}', budget - total, namespace)
    if not kept:
        return
    size = WatchProgress.BUCKET_SECONDS
    with transaction.atomic():
        row, _ = WatchProgress.objects.select_for_update().get_or_create(
            user=user, video_type=label, video_id=video_id
        )
        buckets = list(row.buckets or [])
        run_key = f'vidlock:progress:run:{namespace}:{user.pk}:{label}:{video_id}'
        try:
            run = cache.get(run_key)
        except Exception:
            run = None
        for start, stop in kept:
            # The player cuts a continuous play into pieces at each heartbeat;
            # a piece that picks up where the last one stopped continues it,
            # so a bucket split across two heartbeats still counts, once.
            if run and abs(start - run['end']) <= JOIN_GAP:
                run['end'] = max(run['end'], stop)
            else:
                run = {'start': start, 'end': stop, 'counted': []}
            for index in _covered(run['start'], run['end'], size, duration):
                if index in run['counted']:
                    continue
                run['counted'].append(index)
                if len(buckets) <= index:
                    buckets.extend([0] * (index + 1 - len(buckets)))
                buckets[index] += 1
        with contextlib.suppress(Exception):
            cache.set(run_key, run, 3600)
        row.buckets = buckets
        row.seconds_watched += total
        if duration:
            row.duration = duration
        if isinstance(position, (int, float)) and math.isfinite(position) and position >= 0:
            row.position = float(position)
        row.save()


def _covered(start: float, stop: float, size: int, duration: float) -> list[int]:
    """Buckets that ``[start, stop]`` covers for at least MIN_OVERLAP seconds
    (the video's last, shorter bucket needs less)."""
    found = []
    for index in range(int(start // size), int(max(start, stop - 1e-6) // size) + 1):
        overlap = min(stop, (index + 1) * size) - max(start, index * size)
        length = min(size, duration - index * size) if duration else size
        if overlap >= min(MIN_OVERLAP, length) - 1e-6:
            found.append(index)
    return found


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
