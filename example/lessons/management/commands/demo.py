"""Accounts and a sealed sample lesson, so the demo has something to play.

python manage.py demo
"""

import os
import shutil
import subprocess
import tempfile

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from lessons.models import Lesson
from vidlock import packager
from vidlock.pipeline import seal
from vidlock.storage import default_storage

NARRATION = (
    'Welcome to the lesson on thin lenses. '
    'A converging lens brings parallel rays of light together at the focal point. '
    'The distance from the lens to that point is the focal length. '
    'The lens formula links the object distance, the image distance and the focal length. '
    'One over f equals one over u plus one over v. '
    'Magnification is the image distance divided by the object distance. '
    'Try the three exercises at the end before the next lesson.'
)


class Command(BaseCommand):
    help = 'Create teacher/student accounts (password "demo") and a sealed sample lesson.'

    def handle(self, *args, **opts):
        User = get_user_model()
        for name, staff in (('teacher', True), ('student', False)):
            user, _ = User.objects.get_or_create(
                username=name, defaults={'is_staff': staff, 'is_superuser': staff}
            )
            user.set_password('demo')
            user.save()
        self.stdout.write('accounts: teacher / demo (staff), student / demo')

        if Lesson.objects.exists():
            self.stdout.write('a lesson exists already')
            return
        ffmpeg = packager.ffmpeg_binary()
        if not ffmpeg:
            raise CommandError('ffmpeg is not installed: install it to seal videos (https://ffmpeg.org).')
        with tempfile.TemporaryDirectory(prefix='vidlock-demo-') as work:
            clip = os.path.join(work, 'sample.mp4')
            # A narrated lesson when espeak-ng is around (captions and search
            # then have words to find), a tone otherwise.
            voice = os.path.join(work, 'voice.wav')
            if shutil.which('espeak-ng'):
                subprocess.run(['espeak-ng', '-v', 'en', '-s', '145', '-w', voice, NARRATION], check=True)
                audio = ['-i', voice, '-af', 'apad']
            else:
                audio = ['-f', 'lavfi', '-i', 'sine=frequency=330:duration=150']
            subprocess.run(
                [
                    ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
                    '-f', 'lavfi', '-i', 'testsrc2=duration=150:size=854x480:rate=25', *audio,
                    '-t', '150', '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', clip,
                ],
                check=True,
            )  # fmt: skip
            key = 'uploads/sample.mp4'
            default_storage().upload(clip, key, 'video/mp4')
        lesson = Lesson.objects.create(
            title='Sample lesson (2½ minutes)', video_key=key, sealed_state='pending'
        )
        state = seal(Lesson, lesson.pk, key)
        lesson.refresh_from_db()
        self.stdout.write(f'sample lesson: {state}, {lesson.key_count} keys')
        self.stdout.write('now: python manage.py runserver, then http://127.0.0.1:8000/')
