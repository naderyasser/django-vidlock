"""Pipeline outcomes, management commands, system checks, admin, template tag
and the Django storage adapter."""

import io
import os
from unittest import mock

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.files.storage import FileSystemStorage, InMemoryStorage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.dispatch import receiver
from django.template import Context, Template
from django.test import RequestFactory

from tests.testapp import storage
from tests.testapp.admin import LessonAdmin
from tests.testapp.models import Lesson
from vidlock import checks, keys, packager, signals
from vidlock.pipeline import seal
from vidlock.storage import DjangoStorage

pytestmark = pytest.mark.django_db

SOURCE = 'lessons/1/upload.mp4'


@pytest.fixture
def lesson(sample):
    storage.OBJECTS[SOURCE] = sample
    return Lesson.objects.create(title='Lenz', video_key=SOURCE, sealed_state='pending')


def run(*args):
    out = io.StringIO()
    call_command(*args, stdout=out, stderr=out)
    return out.getvalue()


class TestPipeline:
    def test_the_key_is_stored_encrypted(self, lesson):
        assert seal(Lesson, lesson.pk, SOURCE) == 'sealed'
        lesson.refresh_from_db()
        assert keys.is_wrapped(lesson.sealed_key)
        assert len(lesson.content_key()) == 16
        assert 24 < lesson.sealed_duration < 26

    def test_every_outcome_is_signalled(self, lesson):
        seen = []

        @receiver(signals.seal_finished, sender=Lesson, weak=False)
        def listen(sender, pk, state, error, video_key, **kwargs):
            seen.append((pk, state, video_key))

        try:
            seal(Lesson, lesson.pk, SOURCE)
            seal(Lesson, lesson.pk, SOURCE)  # the row moved on: stale
        finally:
            signals.seal_finished.disconnect(listen, sender=Lesson)
        lesson.refresh_from_db()
        assert seen == [(lesson.pk, 'sealed', lesson.video_key), (lesson.pk, 'stale', lesson.video_key)]

    def test_keep_source(self, lesson, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'KEEP_SOURCE': True}
        assert seal(Lesson, lesson.pk, SOURCE) == 'sealed'
        assert storage.DELETED == []

    def test_ten_bit_h264_is_skipped(self, tmp_path):
        source = tmp_path / 'a.mp4'
        source.write_bytes(b'x')
        storage.OBJECTS['v/a.mp4'] = str(source)
        lesson = Lesson.objects.create(title='x', video_key='v/a.mp4')
        with (
            mock.patch('vidlock.packager.available', return_value=True),
            mock.patch('vidlock.packager.probe', return_value=packager.Probe('h264', 'aac', 'yuv420p10le')),
        ):
            assert seal(Lesson, lesson.pk, 'v/a.mp4') == 'skipped'
        lesson.refresh_from_db()
        assert '8-bit' in lesson.sealed_error


class TestCommands:
    def test_seal_takes_unsealed_rows(self, lesson):
        Lesson.objects.create(title='webm', video_key='x/y.webm')
        assert 'would seal lessons/1/upload.mp4' in run('vidlock_seal', '--dry-run')
        out = run('vidlock_seal', 'testapp.Lesson')
        assert f'testapp.Lesson {lesson.pk}: sealed' in out and 'not a sealable upload' in out
        lesson.refresh_from_db()
        assert lesson.is_sealed
        assert '1 sealed' not in run('vidlock_seal')

    def test_seal_can_queue(self, lesson, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'ENQUEUE_SEAL': 'tests.test_integration.enqueue'}
        QUEUED.clear()
        assert '1 queued' in run('vidlock_seal', '--queue')
        assert [(Lesson, lesson.pk, SOURCE)] == QUEUED

    def test_seal_refuses_a_model_without_the_mixin(self):
        with pytest.raises(CommandError):
            run('vidlock_seal', 'auth.User')

    def test_rewrap_encrypts_0_1_keys_and_rotates(self, settings):
        lesson = Lesson.objects.create(
            title='old',
            video_key='a.ts',
            sealed_state='sealed',
            sealed_playlist='#EXTINF:1.0,\n',
            sealed_key=b'k' * 16,
        )
        assert 'need vidlock_rewrap' in run('vidlock_status')
        assert 'would re-encrypt 1' in run('vidlock_rewrap', '--dry-run')
        assert 're-encrypted 1' in run('vidlock_rewrap')
        lesson.refresh_from_db()
        assert keys.is_wrapped(lesson.sealed_key) and lesson.content_key() == b'k' * 16

        settings.VIDLOCK = {**settings.VIDLOCK, 'KEY_ENCRYPTION_KEYS': ['fresh', settings.SECRET_KEY]}
        assert 're-encrypted 1' in run('vidlock_rewrap')
        settings.VIDLOCK = {**settings.VIDLOCK, 'KEY_ENCRYPTION_KEYS': ['fresh']}
        lesson.refresh_from_db()
        assert lesson.content_key() == b'k' * 16
        assert '1 sealed' in run('vidlock_status')

    def test_export_gives_back_a_playable_mp4(self, lesson, tmp_path):
        seal(Lesson, lesson.pk, SOURCE)
        lesson.refresh_from_db()
        # MemoryStorage keeps uploads as bytes; downloads read paths.
        body, _ = storage.OBJECTS[lesson.video_key]
        sealed_file = tmp_path / 'sealed.ts'
        sealed_file.write_bytes(body)
        storage.OBJECTS[lesson.video_key] = str(sealed_file)
        out = tmp_path / 'out.mp4'
        run('vidlock_export', 'testapp.Lesson', str(lesson.pk), str(out))
        assert tuple(packager.probe(str(out))) == ('h264', 'aac')

    def test_export_refuses_an_unsealed_video(self, lesson, tmp_path):
        with pytest.raises(CommandError):
            run('vidlock_export', 'testapp.Lesson', str(lesson.pk), str(tmp_path / 'x.mp4'))


QUEUED = []


def enqueue(model, pk, key):
    QUEUED.append((model, pk, key))


class TestChecks:
    def test_a_typo_is_reported(self, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'TOKEN_TTLL': 5}
        assert 'vidlock.W001' in [p.id for p in checks.check_settings(None)]

    def test_a_backend_that_cannot_be_imported_is_an_error(self, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'BACKEND': 'nowhere.Backend'}
        assert 'vidlock.E001' in [p.id for p in checks.check_settings(None)]

    def test_missing_ffmpeg_is_a_warning(self):
        with mock.patch('vidlock.packager.available', return_value=False):
            assert 'vidlock.W004' in [p.id for p in checks.check_settings(None)]

    def test_the_test_settings_are_clean(self, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'KEY_ENCRYPTION_KEYS': ['x']}
        assert checks.check_settings(None) == []


class TestAdmin:
    def admin(self):
        model_admin = LessonAdmin(Lesson, AdminSite())
        model_admin.message_user = mock.Mock()
        return model_admin

    def test_seal_again(self, lesson):
        request = RequestFactory().post('/')
        request.user = get_user_model()(is_superuser=True)
        model_admin = self.admin()
        model_admin.seal_again(request, Lesson.objects.all())
        lesson.refresh_from_db()
        assert lesson.is_sealed
        assert model_admin.seal_status(lesson) == 'Sealed'
        assert model_admin.sealed_duration_display(lesson) == '0:25'

    def test_the_change_page_renders(self, admin_client, lesson):
        response = admin_client.get(f'/admin/testapp/lesson/{lesson.pk}/change/')
        assert response.status_code == 200
        assert 'sealed_playlist' not in response.content.decode()
        assert admin_client.get('/admin/testapp/lesson/').status_code == 200


class TestTemplateTag:
    def render(self):
        return Template('{% load vidlock %}{% vidlock_player %}').render(Context())

    def test_the_bundled_hls_js_by_default(self):
        html = self.render()
        assert 'src="/static/vidlock/player.js"' in html
        assert 'data-hls-src="/static/vidlock/vendor/hls.light.min.js"' in html

    def test_a_cdn_with_integrity(self, settings):
        settings.VIDLOCK = {
            **settings.VIDLOCK,
            'HLS_JS_URL': 'https://cdn.example/hls.js',
            'HLS_JS_INTEGRITY': 'sha384-abc',
        }
        html = self.render()
        assert 'data-hls-src="https://cdn.example/hls.js"' in html and 'sha384-abc' in html

    def test_the_bundled_file_ships(self):
        import vidlock

        folder = os.path.join(os.path.dirname(vidlock.__file__), 'static', 'vidlock', 'vendor')
        assert os.path.getsize(os.path.join(folder, 'hls.light.min.js')) > 100_000
        assert os.path.exists(os.path.join(folder, 'hls.js.LICENSE'))


class TestDjangoStorage:
    def test_round_trip(self, tmp_path):
        backend = InMemoryStorage()
        adapter = DjangoStorage(backend=backend)
        source = tmp_path / 'a.ts'
        source.write_bytes(b'sealed bytes')
        name = adapter.upload(str(source), 'lessons/a.ts', 'video/mp2t')
        assert backend.exists(name)
        adapter.download(name, str(tmp_path / 'b.ts'))
        assert (tmp_path / 'b.ts').read_bytes() == b'sealed bytes'
        assert adapter.signed_url(name, 60)
        assert adapter.delete(name) and not backend.exists(name)

    def test_a_public_file_system_is_refused(self, tmp_path):
        from django.core.exceptions import ImproperlyConfigured

        adapter = DjangoStorage(backend=FileSystemStorage(location=str(tmp_path)))
        with pytest.raises(ImproperlyConfigured):
            adapter.signed_url('a.ts', 60)
