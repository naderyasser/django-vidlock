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
            subprocess.run(
                [
                    ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
                    '-f', 'lavfi', '-i', 'testsrc2=duration=150:size=854x480:rate=25',
                    '-f', 'lavfi', '-i', 'sine=frequency=330:duration=150',
                    '-c:v', 'libx264', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', '-shortest', clip,
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
        if shutil.which('open') is None:
            self.stdout.write('now: python manage.py runserver, then http://127.0.0.1:8000/')
