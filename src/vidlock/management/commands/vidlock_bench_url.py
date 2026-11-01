"""Print a sealed video's playlist URL for ``scripts/vidlock_bench.py``.

    manage.py vidlock_bench_url courses.Lesson 42 --user bench-account --base https://courses.example

The URL carries an app-channel token for that account (no cookie needed,
like a phone app's), lives ``TOKEN_TTL`` seconds, and opens only the
playlist: the benchmark fetches byte ranges of the encrypted file and never
a key. Use an account of your own made for measuring; its fetches count
like any viewer's.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse

from vidlock import conf, tokens
from vidlock.models import sealed_models
from vidlock.playlist import with_token


class Command(BaseCommand):
    help = "Print a sealed video's playlist URL (token included) for scripts/vidlock_bench.py."

    def add_arguments(self, parser):
        parser.add_argument('model', metavar='app_label.Model')
        parser.add_argument('pk')
        parser.add_argument('--user', required=True, help='Username of the account to measure as.')
        parser.add_argument('--base', default='', help='Your site, e.g. https://courses.example')

    def handle(self, *args, model, pk, user, base, **opts):
        try:
            (model_class,) = sealed_models([model])
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        video = model_class._default_manager.filter(pk=pk).first()
        if video is None or not video.is_sealed:
            raise CommandError(f'{model} {pk} is not a sealed video.')
        User = get_user_model()
        account = User._default_manager.filter(**{User.USERNAME_FIELD: user}).first()
        if account is None:
            raise CommandError(f'No user {user!r}.')
        backend = conf.load('BACKEND')
        if backend is not None and not backend().can_watch(account, video):
            raise CommandError(f'{user} may not watch {model} {pk} (can_watch() says no).')
        token = tokens.sign(video.pk, account.pk, tokens.APP, tokens.account_fingerprint(account))
        url = with_token(base.rstrip('/') + reverse('vidlock:playlist', args=[video.pk]), token)
        self.stdout.write(url)
        self.stderr.write(f'valid for {conf.get("TOKEN_TTL")} s')
