"""A lesson digest for teachers, and reminders for students. Run it daily.

    manage.py vidlock_insights                       # print the digest of every sealed video
    manage.py vidlock_insights courses.Lesson --pk 42
    manage.py vidlock_insights --send                # to SealedBackend.teachers(video)
    manage.py vidlock_insights --nudge               # remind students who have not started

Sending goes through VIDLOCK['NOTIFIER'] (email, WhatsApp, webhook).
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils.translation import gettext as _

from vidlock import conf, insights, notify
from vidlock.models import sealed_models


class Command(BaseCommand):
    help = "Insights per lesson (who hasn't started, where they stop, what they replay); send or nudge."

    def add_arguments(self, parser):
        parser.add_argument('models', nargs='*', metavar='app_label.Model')
        parser.add_argument('--pk', action='append')
        parser.add_argument('--days', type=int, default=7, help='Quiet this long counts as stalled.')
        parser.add_argument('--send', action='store_true', help='Send each digest to the lesson\'s teachers.')
        parser.add_argument('--nudge', action='store_true', help='Remind students who have not started.')

    def handle(self, *args, **opts):
        try:
            models = sealed_models(opts['models'])
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        cls = conf.load('BACKEND')
        backend = cls() if cls else None
        if (opts['send'] or opts['nudge']) and conf.get('NOTIFIER') is None:
            raise CommandError("--send and --nudge need VIDLOCK['NOTIFIER'].")
        sent = nudged = 0
        for model in models:
            rows = model._default_manager.filter(sealed_state=model.STATE_SEALED)
            if opts['pk']:
                rows = rows.filter(pk__in=opts['pk'])
            for video in rows.iterator():
                text = insights.digest([video], opts['days'])
                self.stdout.write(text)
                if opts['send'] and backend is not None:
                    sent += notify.send(backend.teachers(video), _('Lesson report: %s') % video, text)
                if opts['nudge']:
                    waiting = insights.for_video(video, opts['days'])['not_started']
                    nudged += notify.send(
                        waiting,
                        _('A lesson is waiting for you'),
                        _('You have not started "%s" yet.') % video,
                    )
        if opts['send'] or opts['nudge']:
            self.stdout.write(self.style.SUCCESS(f'{sent} digest(s) sent, {nudged} reminder(s) sent'))
