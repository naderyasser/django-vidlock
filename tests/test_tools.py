"""vidlock_doctor and the development storage."""

import io
import time
from unittest import mock

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import include, path

from vidlock.contrib import devstorage

pytestmark = pytest.mark.django_db

urlpatterns = [path('dev-media/', include('vidlock.contrib.devstorage'))]


class TestDoctor:
    def test_a_healthy_setup(self, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'KEY_ENCRYPTION_KEYS': ['x']}
        out = io.StringIO()
        call_command('vidlock_doctor', '--offline', stdout=out)
        text = out.getvalue()
        assert 'vidlock is ready.' in text
        assert 'seal + unseal work' in text or 'ffmpeg' in text
        assert 'LocMemCache is per process' in text, 'it warns about the per-process cache'

    def test_a_broken_backend_fails(self, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'BACKEND': 'vidlock.backend.SealedBackend'}
        out = io.StringIO()
        with pytest.raises(CommandError):
            call_command('vidlock_doctor', '--offline', stdout=out)
        assert 'does not implement get_video, can_watch' in out.getvalue()


class TestDevStorage:
    @pytest.fixture(autouse=True)
    def root(self, tmp_path, settings):
        settings.DEBUG = True
        settings.ROOT_URLCONF = 'tests.test_tools'
        settings.VIDLOCK = {**settings.VIDLOCK, 'DEV_STORAGE_ROOT': str(tmp_path / 'store')}
        self.tmp = tmp_path

    def put(self, body=b'0123456789' * 100):
        source = self.tmp / 'in.ts'
        source.write_bytes(body)
        storage = devstorage.DevStorage()
        storage.upload(str(source), 'lessons/1/a.ts', 'video/mp2t')
        return storage

    def test_signed_range_reads(self, client):
        url = self.put().signed_url('lessons/1/a.ts', 60)
        answer = client.get(url, HTTP_RANGE='bytes=10-19')
        assert answer.status_code == 206 and answer['Content-Range'] == 'bytes 10-19/1000'
        assert b''.join(answer.streaming_content) == b'0123456789'
        assert client.get(url).status_code == 200

    def test_an_expired_or_forged_link_is_refused(self, client):
        url = self.put().signed_url('lessons/1/a.ts', 60)
        with mock.patch('django.core.signing.time.time', return_value=time.time() + 120):
            assert client.get(url).status_code == 403
        assert client.get('/dev-media/forged').status_code == 403

    def test_no_way_out_of_the_folder(self, client):
        from django.core import signing

        token = signing.dumps({'k': '../../etc/passwd', 'ttl': 60}, salt='vidlock.devstorage')
        assert client.get(f'/dev-media/{token}').status_code == 404

    def test_delete(self):
        storage = self.put()
        assert storage.delete('lessons/1/a.ts') and not storage.delete('lessons/1/a.ts')

    def test_it_refuses_production(self, settings):
        settings.DEBUG = False
        with pytest.raises(ImproperlyConfigured):
            devstorage.DevStorage()
