"""Find the viewer behind a watermark code seen in a leaked recording.

manage.py vidlock_trace K7QMZ4
manage.py vidlock_trace K7QMZ4 --day 2026-09-25
manage.py vidlock_trace K7QMZ4 --days 365
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

    def handle(self, *args, code, day, days, **opts):
        if day:
            try:
                candidates = [datetime.date.fromisoformat(day)]
            except ValueError as exc:
                raise CommandError(f'--day: {exc}') from exc
        else:
            today = timezone.localdate()
            candidates = [today - datetime.timedelta(days=n) for n in range(days)]
        matches = trace.find(code, candidates)
        if not matches:
            raise CommandError(f'No viewer had code {code.upper()} on those days.')
        users = get_user_model()._default_manager.in_bulk([user_id for user_id, _ in matches])
        for user_id, when in matches:
            self.stdout.write(f'{when.isoformat()}  {users.get(user_id, user_id)}  (pk {user_id})')
