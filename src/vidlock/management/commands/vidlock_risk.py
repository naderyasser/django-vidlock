"""A viewer's suspicion score today, and lifting a suspension.

manage.py vidlock_risk amira@example.com
manage.py vidlock_risk 42 --clear
"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError

from vidlock import risk


class Command(BaseCommand):
    help = "Show a viewer's risk score for today; --clear lifts a suspension and resets it."

    def add_arguments(self, parser):
        parser.add_argument('user', help='Primary key or username.')
        parser.add_argument('--namespace', default='', help='The backend namespace, for multi-tenant sites.')
        parser.add_argument('--clear', action='store_true')

    def handle(self, *args, user, namespace, clear, **opts):
        User = get_user_model()
        found = User._default_manager.filter(**{User.USERNAME_FIELD: user}).first()
        if found is None and user.isdigit():
            found = User._default_manager.filter(pk=user).first()
        if found is None:
            raise CommandError(f'No user {user!r}.')
        if clear:
            risk.clear(found.pk, namespace)
            self.stdout.write(self.style.SUCCESS(f'{found}: suspension lifted, score reset'))
            return
        state = cache.get(f'{risk._PREFIX}:score:{namespace}:{found.pk}:{risk._day()}') or {}
        events = ', '.join(f'{name} x{n}' for name, n in sorted(state.get('events', {}).items())) or 'none'
        suspended = ' — SUSPENDED' if risk.is_suspended(found.pk, namespace) else ''
        self.stdout.write(f'{found}: score {state.get("score", 0):g} today ({events}){suspended}')
