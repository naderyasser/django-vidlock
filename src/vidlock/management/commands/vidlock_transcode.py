"""Re-encode and seal the videos that were left unsealed because their
upload could not be sealed as it was (HEVC, 10-bit, VP9…). Needs
``VIDLOCK['TRANSCODE'] = True``, except for a dry run.

    manage.py vidlock_transcode --dry-run         # size the backlog first
    manage.py vidlock_transcode --queue           # hand them to ENQUEUE_TRANSCODE
    manage.py vidlock_transcode courses.Lesson --pk 12 --pk 13
    manage.py vidlock_transcode --state failed --limit 20

The dry run probes each upload where it lies, through a signed URL, so only
the bytes ffprobe needs leave the bucket (it downloads the file instead when
ffprobe or an HTTP URL is not available). ``--no-probe`` lists what the
database knows and reads nothing from storage.
"""

from __future__ import annotations

import os
import tempfile

from django.core.management.base import BaseCommand, CommandError

from vidlock import conf, packager, pipeline, transcode
from vidlock.models import SealedVideoMixin, Transcode, sealed_models
from vidlock.storage import default_storage

STATES = {
    'skipped': SealedVideoMixin.STATE_SKIPPED,
    'failed': SealedVideoMixin.STATE_FAILED,
    'pending': SealedVideoMixin.STATE_PENDING,
    'none': SealedVideoMixin.STATE_NONE,
}


def clock(seconds: float) -> str:
    seconds = round(seconds)
    hours, rest = divmod(seconds, 3600)
    return f'{hours}:{rest // 60:02d}:{rest % 60:02d}'


def human(size: int | None) -> str:
    if size is None:
        return '?'
    value = float(size)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024 or unit == 'GB':
            return f'{value:.0f} {unit}' if unit == 'B' else f'{value:.1f} {unit}'
        value /= 1024
    return f'{value:.1f} TB'


class Command(BaseCommand):
    help = 'Re-encode and seal videos left unsealed (default: skipped). Try --dry-run first.'

    def add_arguments(self, parser):
        parser.add_argument('models', nargs='*', metavar='app_label.Model')
        parser.add_argument(
            '--state',
            action='append',
            choices=sorted(STATES),
            help='Which rows to take; repeat for several. Default: skipped.',
        )
        parser.add_argument('--pk', action='append', help='Only these primary keys; repeatable.')
        parser.add_argument('--limit', type=int, default=0, help='Stop after this many videos.')
        parser.add_argument('--queue', action='store_true', help="Queue with VIDLOCK['ENQUEUE_TRANSCODE'].")
        parser.add_argument(
            '--dry-run', action='store_true', help='Probe and list what would be re-encoded, with totals.'
        )
        parser.add_argument(
            '--no-probe', action='store_true', help='With --dry-run: read nothing from storage.'
        )

    def handle(self, *args, **opts):
        try:
            models = sealed_models(opts['models'])
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        if not opts['dry_run'] and not transcode.enabled():
            raise CommandError("Set VIDLOCK['TRANSCODE'] = True first (or look with --dry-run).")
        enqueue = conf.load('ENQUEUE_TRANSCODE') if opts['queue'] else None
        if opts['queue'] and enqueue is None:
            raise CommandError("--queue needs VIDLOCK['ENQUEUE_TRANSCODE'].")
        states = [STATES[s] for s in (opts['state'] or ['skipped'])]
        storage = default_storage() if opts['dry_run'] and not opts['no_probe'] else None

        done = 0
        totals = {}
        sized = {'videos': 0, 'seconds': 0.0, 'bytes': 0, 'unknown_size': 0, 'fixable': 0}
        for model in models:
            rows = model._default_manager.filter(sealed_state__in=states).exclude(video_key='')
            if opts['pk']:
                rows = rows.filter(pk__in=opts['pk'])
            for pk, key in rows.order_by('pk').values_list('pk', 'video_key').iterator():
                if opts['limit'] and done >= opts['limit']:
                    break
                label = f'{model._meta.label} {pk}'
                if opts['dry_run']:
                    done += 1
                    self.describe(label, model, pk, key, storage, sized)
                    continue
                if not pipeline.wants(key):
                    self.stdout.write(f'{label}: not a re-encodable upload ({key})')
                    continue
                done += 1
                if enqueue:
                    transcode.queue(model, pk, key)
                    state = 'queued'
                else:
                    model._default_manager.filter(pk=pk, video_key=key).update(
                        sealed_state=model.STATE_PENDING, sealed_error=''
                    )
                    state = transcode.run(model, pk, key)
                totals[state] = totals.get(state, 0) + 1
                self.stdout.write(f'{label}: {state}')
        if opts['dry_run']:
            self.summary(sized)
            return
        summary = ', '.join(f'{n} {state}' for state, n in sorted(totals.items())) or 'nothing to do'
        self.stdout.write(self.style.SUCCESS(summary))

    # -- the dry run ------------------------------------------------------

    def probe(self, key, storage):
        """(Probe or None, size or None) for the object ``key``."""
        size = None
        try:
            size = storage.size(key)
        except Exception:  # NotImplementedError, or storage briefly unreachable
            size = None
        try:
            return packager.probe_url(storage.signed_url(key, 600), key), size
        except Exception:
            pass
        with tempfile.TemporaryDirectory(prefix='vidlock-dry-') as work:
            local = os.path.join(work, 'source' + os.path.splitext(key)[1].lower())
            storage.download(key, local)
            return packager.probe(local), size if size is not None else os.path.getsize(local)

    def describe(self, label, model, pk, key, storage, sized):
        sized['videos'] += 1
        found, size, error = None, None, ''
        if storage is not None:
            try:
                found, size = self.probe(key, storage)
            except Exception as exc:
                error = f'cannot read it: {type(exc).__name__}: {exc}'[:200]
        if found is None:
            known = Transcode.objects.filter(video_type=model._meta.label, video_id=str(pk)).first()
            error = error or (
                model._default_manager.filter(pk=pk).values_list('sealed_error', flat=True).first() or ''
            )
            info = known.source_info if known and known.source_info else '?'
            self.stdout.write(f'{label}  {key}  {info}  {error or "not probed"}')
            if size is None:
                sized['unknown_size'] += 1
            return
        if size is None:
            sized['unknown_size'] += 1
        else:
            sized['bytes'] += size
        sized['seconds'] += found.duration
        if not transcode.needs(found, key):
            plan = 'sealable as it is (no re-encode needed)'
        elif not transcode.can_fix(found):
            plan = f'cannot be fixed: {found.problem or "no picture"}'
        elif found.video_ok:
            plan = f'sound/container only, picture copied: {found.problem or "MKV/WebM"}'
            sized['fixable'] += 1
        else:
            plan = f're-encode: {found.problem}'
            sized['fixable'] += 1
        resolution = f'{found.width}x{found.height}' if found.width else '?x?'
        self.stdout.write(
            f'{label}  {key}  {found.video or "?"}/{found.audio or "-"} {found.pix_fmt or "?"}'
            f'{" HDR" if found.hdr else ""}  {resolution}  {clock(found.duration)}  {human(size)}  {plan}'
        )

    def summary(self, sized):
        line = (
            f'{sized["videos"]} video(s), {sized["fixable"]} to re-encode; '
            f'{clock(sized["seconds"])} of video ({sized["seconds"] / 3600:.1f} h), {human(sized["bytes"])}'
        )
        if sized['unknown_size']:
            line += f' (+{sized["unknown_size"]} of unknown size)'
        self.stdout.write(self.style.SUCCESS(line))
