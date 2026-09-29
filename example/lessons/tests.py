"""The demo end to end, through Django's test client."""

import json
import tempfile
from urllib.parse import urlsplit

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase, override_settings

from lessons.models import Lesson

TMP = tempfile.mkdtemp(prefix='vidlock-demo-test-')


# The test runner turns DEBUG off; the development storage needs it on.
@override_settings(DEBUG=True, VIDLOCK={**settings.VIDLOCK, 'DEV_STORAGE_ROOT': TMP})
class DemoTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with override_settings(DEBUG=True, VIDLOCK={**settings.VIDLOCK, 'DEV_STORAGE_ROOT': TMP}):
            call_command('demo', verbosity=0)

    def setUp(self):
        self.lesson = Lesson.objects.get()

    def test_the_sample_is_sealed_with_rotating_keys(self):
        self.assertTrue(self.lesson.is_sealed)
        self.assertEqual(self.lesson.key_count, 3)

    def test_a_student_gets_a_playlist_a_sealed_key_and_the_bytes(self):
        self.client.login(username='student', password='demo')
        self.assertContains(self.client.get('/'), 'Sample lesson')
        self.assertContains(self.client.get(f'/lessons/{self.lesson.pk}/'), 'vidlock/player.js')
        info = self.client.get(f'/lessons/{self.lesson.pk}/playback/').json()
        self.assertEqual(info['format'], 'hls')
        playlist = self.client.get(info['url']).content.decode()
        media = next(line for line in playlist.splitlines() if line.startswith('/dev-media/'))
        chunk = self.client.get(media, HTTP_RANGE='bytes=0-187')
        self.assertEqual(chunk.status_code, 206)
        self.assertNotEqual(b''.join(chunk.streaming_content)[:1], b'\x47', 'the stored bytes are encrypted')
        path = urlsplit(info['key_url'])
        self.assertEqual(self.client.get(f'{path.path}?{path.query}').status_code, 200)

    def test_the_teacher_sees_who_watched(self):
        self.client.login(username='student', password='demo')
        info = self.client.get(f'/lessons/{self.lesson.pk}/playback/').json()
        beat = urlsplit(info['heartbeat_url'])
        self.client.post(
            f'{beat.path}?{beat.query}', json.dumps({'watched': [[0, 40]]}), content_type='application/json'
        )
        self.client.login(username='teacher', password='demo')
        page = self.client.get(f'/lessons/{self.lesson.pk}/report/')
        self.assertContains(page, '<strong>1</strong> viewers')
        self.assertContains(page, 'student')
