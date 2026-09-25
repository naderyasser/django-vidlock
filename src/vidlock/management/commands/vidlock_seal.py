"""Seal videos already in your database — after installing vidlock, or to
retry the ones that failed.

    manage.py vidlock_seal                       # every model, unsealed + failed
    manage.py vidlock_seal courses.Lesson --state failed
    manage.py vidlock_seal courses.Lesson --pk 12 --pk 13
    manage.py vidlock_seal --queue               # hand them to VIDLOCK['ENQUEUE_SEAL']
"""

from django.core.management.base import BaseCommand, CommandError

from vidlock import conf, pipeline
from vidlock.models import SealedVideoMixin, sealed_models

STATES = {
    'none': SealedVideoMixin.STATE_NONE,
    'pending': SealedVideoMixin.STATE_PENDING,
    'failed': SealedVideoMixin.STATE_FAILED,
    'skipped': SealedVideoMixin.STATE_SKIPPED,
}


class Command(BaseCommand):
    help = 'Seal existing videos (default: those never sealed, pending or failed).'

    def add_arguments(self, parser):
        parser.add_argument('models', nargs='*', metavar='app_label.Model')
        parser.add_argument(
            '--state',
            action='append',
            choices=sorted(STATES),
            help='Which rows to take; repeat for several. Default: none, pending, failed.',
        )
        parser.add_argument('--pk', action='append', help='Only these primary keys; repeatable.')
        parser.add_argument('--limit', type=int, default=0, help='Stop after this many videos.')
        parser.add_argument('--queue', action='store_true', help="Queue with VIDLOCK['ENQUEUE_SEAL'].")
        parser.add_argument('--dry-run', action='store_true', help='List what would be sealed.')

    def handle(self, *args, **opts):
        try:
            models = sealed_models(opts['models'])
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        states = [STATES[s] for s in (opts['state'] or ['none', 'pending', 'failed'])]
        enqueue = conf.load('ENQUEUE_SEAL') if opts['queue'] else None
        if opts['queue'] and enqueue is None:
            raise CommandError("--queue needs VIDLOCK['ENQUEUE_SEAL'].")

        done = 0
        totals = {}
        for model in models:
            rows = model._default_manager.filter(sealed_state__in=states).exclude(video_key='')
            if opts['pk']:
                rows = rows.filter(pk__in=opts['pk'])
            for pk, key in rows.order_by('pk').values_list('pk', 'video_key').iterator():
                if opts['limit'] and done >= opts['limit']:
                    break
                if not pipeline.wants(key):
                    self.stdout.write(f'{model._meta.label} {pk}: not a sealable upload ({key})')
                    continue
                done += 1
                if opts['dry_run']:
                    self.stdout.write(f'{model._meta.label} {pk}: would seal {key}')
                    continue
                if enqueue:
                    model._default_manager.filter(pk=pk, video_key=key).update(
                        sealed_state=model.STATE_PENDING
                    )
                    enqueue(model, pk, key)
                    state = 'queued'
                else:
                    state = pipeline.seal(model, pk, key)
                totals[state] = totals.get(state, 0) + 1
                self.stdout.write(f'{model._meta.label} {pk}: {state}')
        summary = ', '.join(f'{n} {state}' for state, n in sorted(totals.items())) or 'nothing to do'
        self.stdout.write(self.style.SUCCESS(summary))
