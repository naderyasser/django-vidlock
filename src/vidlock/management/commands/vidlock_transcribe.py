"""Transcribe videos sealed before VIDLOCK['TRANSCRIBER'] was set.

    manage.py vidlock_transcribe                     # every sealed video without a transcript
    manage.py vidlock_transcribe courses.Lesson --pk 42 --again

Each one is decrypted to a temporary file on this machine (the worker's
disk, never the bucket), its audio extracted and transcribed, then deleted.
"""

import os
import tempfile

from django.core.management.base import BaseCommand, CommandError

from vidlock import keys, packager, transcribe
from vidlock.models import Transcript, sealed_models
from vidlock.storage import default_storage


class Command(BaseCommand):
    help = 'Transcribe sealed videos that have no transcript yet (captions and search).'

    def add_arguments(self, parser):
        parser.add_argument('models', nargs='*', metavar='app_label.Model')
        parser.add_argument('--pk', action='append', help='Only these primary keys; repeatable.')
        parser.add_argument('--again', action='store_true', help='Also redo videos that have a transcript.')
        parser.add_argument('--limit', type=int, default=0)

    def handle(self, *args, **opts):
        engine = transcribe.transcriber()
        if engine is None:
            raise CommandError("Set VIDLOCK['TRANSCRIBER'] first, e.g. 'vidlock.transcribe.FasterWhisper'.")
        try:
            models = sealed_models(opts['models'])
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        done = 0
        for model in models:
            rows = model._default_manager.filter(sealed_state=model.STATE_SEALED)
            if opts['pk']:
                rows = rows.filter(pk__in=opts['pk'])
            have = set(
                Transcript.objects.filter(video_type=model._meta.label).values_list('video_id', flat=True)
            )
            for video in rows.order_by('pk').iterator():
                if opts['limit'] and done >= opts['limit']:
                    return
                if str(video.pk) in have and not opts['again']:
                    continue
                label = f'{model._meta.label} {video.pk}'
                try:
                    found = self.one(video, engine)
                except Exception as exc:
                    self.stderr.write(f'{label}: {type(exc).__name__}: {exc}')
                    continue
                done += 1
                self.stdout.write(f'{label}: {len(found.segments)} lines ({found.language or "?"})')
        self.stdout.write(self.style.SUCCESS(f'{done} transcribed'))

    def one(self, video, engine):
        with tempfile.TemporaryDirectory(prefix='vidlock-transcribe-') as work:
            ts_path = os.path.join(work, 'media.ts')
            default_storage().download(video.video_key, ts_path)
            clear = packager.unpackage(
                ts_path, video.sealed_playlist, keys.unwrap(video.sealed_key), os.path.join(work, 'clear.mp4')
            )
            audio = transcribe.extract_audio(clear, os.path.join(work, 'audio.wav'))
            return transcribe.transcribe_audio(video, audio, engine)
