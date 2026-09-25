"""Decrypt a sealed video back into a playable MP4 on this machine.

The way back when you need the original — leaving vidlock, moving platform,
or checking a video — without keeping every source upload in the bucket.

    manage.py vidlock_export courses.Lesson 42 lesson-42.mp4
"""

import os
import tempfile

from django.core.management.base import BaseCommand, CommandError

from vidlock import keys, packager
from vidlock.models import sealed_models
from vidlock.storage import default_storage


class Command(BaseCommand):
    help = 'Decrypt one sealed video into a local MP4 (remux, no re-encode).'

    def add_arguments(self, parser):
        parser.add_argument('model', metavar='app_label.Model')
        parser.add_argument('pk')
        parser.add_argument('output', help='Path of the MP4 to write.')

    def handle(self, *args, **opts):
        try:
            (model,) = sealed_models([opts['model']])
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        video = model._default_manager.filter(pk=opts['pk']).first()
        if video is None:
            raise CommandError(f'{model._meta.label} {opts["pk"]} does not exist.')
        if not video.is_sealed:
            label = f'{model._meta.label} {opts["pk"]}'
            raise CommandError(f'{label} is not sealed; its file is {video.video_key}.')
        try:
            key = keys.unwrap(video.sealed_key)
        except keys.KeyUnwrapError as exc:
            raise CommandError(str(exc)) from exc
        with tempfile.TemporaryDirectory(prefix='vidlock-') as work:
            ts_path = os.path.join(work, 'media.ts')
            default_storage().download(video.video_key, ts_path)
            try:
                packager.unpackage(ts_path, video.sealed_playlist, key, os.path.abspath(opts['output']))
            except packager.PackagingError as exc:
                raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f'wrote {opts["output"]}'))
