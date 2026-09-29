"""How many videos are in each state, per model.

manage.py vidlock_status
"""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count

from vidlock import conf, keys, monitor
from vidlock.models import sealed_models


class Command(BaseCommand):
    help = 'Count videos by sealing state, and keys still waiting for vidlock_rewrap.'

    def add_arguments(self, parser):
        parser.add_argument('models', nargs='*', metavar='app_label.Model')

    def handle(self, *args, **opts):
        try:
            models = sealed_models(opts['models'])
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        for model in models:
            counts = {
                row['sealed_state']: row['n']
                for row in model._default_manager.exclude(video_key='')
                .order_by()
                .values('sealed_state')
                .annotate(n=Count('pk'))
            }
            stale_keys = sum(
                1
                for blob in model._default_manager.exclude(sealed_key=None)
                .values_list('sealed_key', flat=True)
                .iterator()
                if keys.needs_rewrap(blob)
            )
            parts = [f'{n} {state or "unsealed"}' for state, n in sorted(counts.items())] or ['no videos']
            if stale_keys:
                parts.append(f'{stale_keys} key(s) need vidlock_rewrap')
            self.stdout.write(f'{model._meta.label}: {", ".join(parts)}')
        mode = 'enforcing' if conf.enforcing() else 'MONITOR MODE (nothing is refused)'
        self.stdout.write(f'protection: {mode}, profile {conf.get("PROFILE")}')
        held = monitor.counts()
        if held:
            listed = ', '.join(f'{rule} x{n}' for rule, n in sorted(held.items(), key=lambda kv: -kv[1]))
            self.stdout.write(f'would have refused today: {listed}')
