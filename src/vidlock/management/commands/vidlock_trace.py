"""Find the viewer behind a watermark code seen in a leaked recording.

manage.py vidlock_trace K7QMZ4
manage.py vidlock_trace K7QMZ4 --day 2026-09-25
manage.py vidlock_trace K7QMZ4 --days 365
manage.py vidlock_trace K7QMZ4 --no-legacy   # skip codes from the first 0.3.0 build

Codes from recordings made under the first 0.3.0 build (SHA-1, before the
algorithm was pinned) are found too, and marked as such.
"""

import datetime

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from vidlock import trace


class Command(BaseCommand):
    help = 'Turn a watermark code back into the viewer and day it was drawn for.'

    def add_arguments(self, parser):
        parser.add_argument('code')
        parser.add_argument('--day', help='The day of the recording, YYYY-MM-DD, if you know it.')
        parser.add_argument('--days', type=int, default=60, help='How many days back to search.')
        parser.add_argument(
            '--no-legacy',
            action='store_true',
            help='Do not try SHA-1 codes from the first 0.3.0 build (recordings made before upgrading).',
        )

    def handle(self, *args, code, day, days, no_legacy=False, **opts):
        if day:
            try:
                candidates = [datetime.date.fromisoformat(day)]
            except ValueError as exc:
                raise CommandError(f'--day: {exc}') from exc
        else:
            today = timezone.localdate()
            candidates = [today - datetime.timedelta(days=n) for n in range(days)]
        found = trace.matches(code, candidates, legacy=not no_legacy)
        if not found:
            raise CommandError(f'No viewer had code {code.upper()} on those days.')
        users = get_user_model()._default_manager.in_bulk([m['user_id'] for m in found])
        for m in found:
            note = (
                '  [legacy SHA-1 code: recorded before the upgrade from the first 0.3.0]'
                if m['legacy']
                else ''
            )
            self.stdout.write(
                f"{m['day'].isoformat()}  {users.get(m['user_id'], m['user_id'])}  (pk {m['user_id']}){note}"
            )
