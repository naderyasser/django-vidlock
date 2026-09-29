"""Who watched a video, how far, and which part they replay.

manage.py vidlock_progress courses.Lesson 42
"""

from django.core.management.base import BaseCommand, CommandError

from vidlock import progress
from vidlock.models import sealed_models

BARS = ' ▁▂▃▄▅▆▇█'


class Command(BaseCommand):
    help = "A video's watch report: viewers, completion and a replay heatmap."

    def add_arguments(self, parser):
        parser.add_argument('model', metavar='app_label.Model')
        parser.add_argument('pk')

    def handle(self, *args, model, pk, **opts):
        try:
            (cls,) = sealed_models([model])
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        video = cls._default_manager.filter(pk=pk).first()
        if video is None:
            raise CommandError(f'{cls._meta.label} {pk} does not exist.')
        data = progress.report(video)
        self.stdout.write(
            f'{data["viewers"]} viewers, {data["completed"]} finished, '
            f'{data["average_completion"]:.0%} watched on average, {data["hours_watched"]} hours in all'
        )
        heat = data['heatmap']
        if any(heat):
            top = max(heat)
            self.stdout.write(''.join(BARS[round(n / top * (len(BARS) - 1))] for n in heat))
            at = data['most_replayed_at']
            self.stdout.write(f'most played around {at // 60}:{at % 60:02d}')
