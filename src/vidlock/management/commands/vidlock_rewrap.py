"""Re-encrypt stored video keys under the first VIDLOCK['KEY_ENCRYPTION_KEYS'].

Run it after putting a new secret first in the list (then, once it reports
nothing left, drop the old one), and once after upgrading from vidlock 0.1,
whose keys were stored raw.

    manage.py vidlock_rewrap
    manage.py vidlock_rewrap courses.Lesson --dry-run
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from vidlock import keys
from vidlock.models import sealed_models


class Command(BaseCommand):
    help = 'Encrypt every stored video key under the current key-encryption key.'

    def add_arguments(self, parser):
        parser.add_argument('models', nargs='*', metavar='app_label.Model')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        try:
            models = sealed_models(opts['models'])
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        changed = failed = 0
        for model in models:
            rows = model._default_manager.exclude(sealed_key=None).values_list('pk', 'sealed_key')
            for pk, blob in rows.iterator():
                if not keys.needs_rewrap(blob):
                    continue
                try:
                    clear = keys.unwrap(blob)
                except keys.KeyUnwrapError:
                    failed += 1
                    self.stderr.write(f'{model._meta.label} {pk}: no configured secret opens this key')
                    continue
                changed += 1
                if opts['dry_run']:
                    continue
                with transaction.atomic():
                    # Only if nobody re-sealed the row in the meantime.
                    model._default_manager.filter(pk=pk, sealed_key=blob).update(sealed_key=keys.wrap(clear))
        verb = 'would re-encrypt' if opts['dry_run'] else 're-encrypted'
        self.stdout.write(self.style.SUCCESS(f'{verb} {changed} key(s)'))
        if failed:
            raise CommandError(f'{failed} key(s) could not be opened with the configured secrets.')
