"""What a teacher should know about a lesson, from watch progress.

    from vidlock import insights

    found = insights.for_video(lesson)
    found['not_started']     # enrolled viewers who never pressed play
    found['stalled']         # started, stopped short, gone quiet for days
    found['drop_off_at']     # where most of those who stopped, stopped (seconds)
    found['confusing']       # [(seconds, times replayed vs. the lesson's median)]
    print(insights.digest([lesson]))

"Enrolled" comes from ``SealedBackend.audience(video)``; without it, only
viewers who started count. ``manage.py vidlock_insights --send`` sends the
digest to each lesson's teachers (``SealedBackend.teachers(video)``) through
``VIDLOCK['NOTIFIER']`` — email, WhatsApp or a webhook, see ``vidlock.notify``
— and ``--nudge`` reminds the students who have not started.
"""

from __future__ import annotations

import datetime
import statistics

from django.utils import timezone
from django.utils.translation import gettext as _

from vidlock import conf
from vidlock.models import WatchProgress

#: Watched at least this share of the lesson: finished.
FINISHED = 0.9
#: A stretch replayed this many times more than the lesson's median is "confusing".
REPLAY_RATIO = 1.8
#: ...and needs at least this many viewers to mean anything.
MIN_VIEWERS = 3


def _backend():
    cls = conf.load('BACKEND')
    return cls() if cls else None


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    return f'{seconds // 60}:{seconds % 60:02d}'


def for_video(video, quiet_days: int = 7) -> dict:
    rows = list(
        WatchProgress.objects.filter(video_type=video._meta.label, video_id=str(video.pk)).select_related(
            'user'
        )
    )
    backend = _backend()
    audience = backend.audience(video) if backend is not None else None
    watched_by = {row.user_id for row in rows}
    not_started = [u for u in audience if u.pk not in watched_by] if audience is not None else []
    cutoff = timezone.now() - datetime.timedelta(days=quiet_days)
    unfinished = [row for row in rows if row.completion < FINISHED]
    stalled = [row.user for row in unfinished if row.last_seen < cutoff]

    size = WatchProgress.BUCKET_SECONDS
    stops = [int(row.position // size) for row in unfinished if row.position > 0]
    drop_off_at = statistics.mode(stops) * size if stops else None

    confusing = []
    if len(rows) >= MIN_VIEWERS:
        length = max(len(row.buckets) for row in rows)
        per_bucket = [0] * length
        for row in rows:
            for index, count in enumerate(row.buckets):
                # A viewer's replays: plays of this stretch beyond the first.
                per_bucket[index] += max(0, count - 1)
        nonzero = [n for n in per_bucket if n]
        baseline = max(1.0, statistics.median(per_bucket) if per_bucket else 0)
        if nonzero:
            for index, replays in enumerate(per_bucket):
                if replays >= MIN_VIEWERS and replays / baseline >= REPLAY_RATIO:
                    confusing.append((index * size, round(replays / baseline, 1)))
        confusing.sort(key=lambda item: -item[1])

    finished = sum(1 for row in rows if row.completion >= FINISHED)
    return {
        'video': video,
        'audience': len(audience) if audience is not None else None,
        'started': len(rows),
        'finished': finished,
        'not_started': not_started,
        'stalled': stalled,
        'drop_off_at': drop_off_at,
        'confusing': confusing[:3],
    }


def digest(videos, quiet_days: int = 7) -> str:
    """A short plain-text report for a teacher, in the active language."""
    lines = []
    for video in videos:
        found = for_video(video, quiet_days)
        lines.append(f'• {video}')
        if found['audience'] is not None:
            lines.append(
                _('  %(finished)s of %(audience)s finished, %(started)s started, %(waiting)s not yet')
                % {
                    'finished': found['finished'],
                    'audience': found['audience'],
                    'started': found['started'],
                    'waiting': len(found['not_started']),
                }
            )
        else:
            lines.append(
                _('  %(finished)s finished of %(started)s who started')
                % {'finished': found['finished'], 'started': found['started']}
            )
        if found['stalled']:
            lines.append(
                _('  %(count)s stopped and have not come back for %(days)s days')
                % {'count': len(found['stalled']), 'days': quiet_days}
            )
        if found['drop_off_at'] is not None:
            lines.append(_('  most who stop, stop around %(at)s') % {'at': _clock(found['drop_off_at'])})
        for at, ratio in found['confusing']:
            lines.append(
                _('  %(at)s is replayed %(ratio)sx more than the rest: worth explaining again?')
                % {'at': _clock(at), 'ratio': ratio}
            )
    return '\n'.join(lines)
