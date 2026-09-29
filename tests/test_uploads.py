"""Direct uploads: the browser PUTs to the bucket, Django signs and checks."""

import json
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core import signing
from django.urls import include, path

from tests.testapp import storage
from vidlock import uploads
from vidlock.contrib import devstorage
from vidlock.storage import S3Storage

pytestmark = pytest.mark.django_db

urlpatterns = [
    path('sealed/', include('vidlock.urls')),
    path('dev-media/', include('vidlock.contrib.devstorage')),
]


@pytest.fixture
def teacher():
    return get_user_model().objects.create_user('teacher', password='x', is_staff=True)


@pytest.fixture
def student():
    return get_user_model().objects.create_user('student', password='x')


def _uploaded(started, body=b'video bytes'):
    storage.OBJECTS[started['key']] = (body, 'video/mp4')


class TestStartAndFinish:
    def test_a_round_trip(self, teacher):
        started = uploads.start(teacher, 'Lesson 1.MP4', 1234)
        assert started['key'].startswith('uploads/') and started['key'].endswith('.mp4')
        assert started['method'] == 'PUT'
        assert started['headers'] == {'Content-Type': 'video/mp4'}
        _uploaded(started)
        assert uploads.finish(teacher, started['ticket']) == started['key']

    def test_the_content_type_comes_from_the_extension(self, teacher):
        started = uploads.start(teacher, 'clip.mov', 10)
        assert storage.TARGETS[started['key']] == 'video/quicktime'

    @pytest.mark.parametrize('name', ['notes.pdf', 'movie.mkv', 'noextension', ''])
    def test_only_what_vidlock_can_seal(self, teacher, name):
        with pytest.raises(uploads.UploadRefused):
            uploads.start(teacher, name, 10)

    @pytest.mark.parametrize('size', [0, -5, 'lots', None])
    def test_a_real_size(self, teacher, size):
        with pytest.raises(uploads.UploadRefused):
            uploads.start(teacher, 'a.mp4', size)

    def test_the_size_limit(self, teacher, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'MAX_UPLOAD_BYTES': 3 * 1024**2}
        with pytest.raises(uploads.UploadRefused, match='limit is 3 MB'):
            uploads.start(teacher, 'a.mp4', 4 * 1024**2)

    def test_an_oversize_object_is_deleted_at_finish(self, teacher, settings):
        # A presigned PUT cannot cap the size: the browser may send more than it said.
        started = uploads.start(teacher, 'a.mp4', 10)
        settings.VIDLOCK = {**settings.VIDLOCK, 'MAX_UPLOAD_BYTES': 5}
        _uploaded(started, b'x' * 50)
        with pytest.raises(uploads.UploadRefused):
            uploads.finish(teacher, started['ticket'])
        assert started['key'] in storage.DELETED

    def test_nothing_uploaded(self, teacher):
        started = uploads.start(teacher, 'a.mp4', 10)
        with pytest.raises(uploads.UploadRefused, match='did not finish'):
            uploads.finish(teacher, started['ticket'])

    def test_someone_elses_ticket(self, teacher, student):
        started = uploads.start(teacher, 'a.mp4', 10)
        _uploaded(started)
        with pytest.raises(uploads.UploadRefused):
            uploads.finish(student, started['ticket'])

    def test_a_forged_ticket(self, teacher):
        forged = signing.dumps({'k': 'lessons/someone-elses.mp4', 'u': teacher.pk}, salt='other')
        with pytest.raises(uploads.UploadRefused):
            uploads.finish(teacher, forged)
        with pytest.raises(uploads.UploadRefused):
            uploads.finish(teacher, '')

    def test_a_ticket_works_once(self, teacher):
        started = uploads.start(teacher, 'a.mp4', 10)
        _uploaded(started)
        uploads.finish(teacher, started['ticket'])
        with pytest.raises(uploads.UploadRefused, match='already'):
            uploads.finish(teacher, started['ticket'])

    def test_an_old_ticket(self, teacher):
        started = uploads.start(teacher, 'a.mp4', 10)
        _uploaded(started)
        with mock.patch('vidlock.uploads.TICKET_MAX_AGE', -1), pytest.raises(uploads.UploadRefused):
            uploads.finish(teacher, started['ticket'])


class TestView:
    def _post(self, client, body):
        return client.post('/sealed/upload', json.dumps(body), content_type='application/json')

    def test_staff_get_a_target(self, client, teacher):
        client.force_login(teacher)
        response = self._post(client, {'name': 'a.mp4', 'size': 100})
        assert response.status_code == 200
        assert set(response.json()) == {'ticket', 'key', 'method', 'url', 'headers', 'expires_in'}

    def test_students_do_not(self, client, student):
        client.force_login(student)
        assert self._post(client, {'name': 'a.mp4', 'size': 100}).status_code == 403

    def test_anonymous_do_not(self, client):
        assert self._post(client, {'name': 'a.mp4', 'size': 100}).status_code == 403

    def test_a_refusal_says_why(self, client, teacher):
        client.force_login(teacher)
        response = self._post(client, {'name': 'a.exe', 'size': 100})
        assert response.status_code == 400 and 'MP4' in response.json()['error']

    def test_it_needs_post_and_csrf(self, teacher):
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        client.force_login(teacher)
        assert client.get('/sealed/upload').status_code == 405
        assert self._post(client, {'name': 'a.mp4', 'size': 1}).status_code == 403

    def test_a_storage_without_direct_uploads(self, client, teacher, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'STORAGE': 'vidlock.storage.Storage'}
        client.force_login(teacher)
        assert self._post(client, {'name': 'a.mp4', 'size': 1}).status_code == 501

    def test_the_backend_decides(self, client, student):
        client.force_login(student)
        with mock.patch('tests.testapp.backend.LessonBackend.can_upload', return_value=True):
            assert self._post(client, {'name': 'a.mp4', 'size': 1}).status_code == 200


class TestDevStorage:
    @pytest.fixture(autouse=True)
    def _dev(self, settings, tmp_path, client, teacher):
        settings.DEBUG = True
        settings.ROOT_URLCONF = 'tests.test_uploads'
        settings.VIDLOCK = {
            **settings.VIDLOCK,
            'STORAGE': 'vidlock.contrib.devstorage.DevStorage',
            'DEV_STORAGE_ROOT': str(tmp_path),
        }
        client.force_login(teacher)

    def test_the_whole_way(self, client, teacher, tmp_path):
        started = client.post(
            '/sealed/upload', json.dumps({'name': 'a.mp4', 'size': 5}), content_type='application/json'
        ).json()
        put = client.generic('PUT', started['url'], b'hello', content_type='video/mp4')
        assert put.status_code == 200
        assert (tmp_path / started['key']).read_bytes() == b'hello'
        assert uploads.finish(teacher, started['ticket']) == started['key']

    def test_a_bad_put_link(self, client):
        assert client.generic('PUT', '/dev-media/put/nonsense', b'x').status_code == 403
        token = signing.dumps({'k': '../../escape.mp4', 'ttl': 60}, salt='vidlock.devstorage.put')
        assert client.generic('PUT', f'/dev-media/put/{token}', b'x').status_code == 404

    def test_only_put(self, client):
        target = devstorage.DevStorage().upload_target('uploads/a.mp4', 'video/mp4', 60)
        assert client.get(target['url']).status_code == 405


class TestS3:
    @pytest.fixture(autouse=True)
    def _botocore(self):
        pytest.importorskip('botocore')

    def test_a_presigned_put_and_head(self):
        s3 = S3Storage(bucket='b')
        s3._client = mock.Mock()
        s3._client.generate_presigned_url.return_value = 'https://r2.example/b/k?X-Amz-Signature=1'
        s3._client.head_object.return_value = {'ContentLength': 42}
        target = s3.upload_target('uploads/k.mp4', 'video/mp4', 10**9)
        s3._client.generate_presigned_url.assert_called_once_with(
            'put_object',
            Params={'Bucket': 'b', 'Key': 'uploads/k.mp4', 'ContentType': 'video/mp4'},
            ExpiresIn=7 * 24 * 3600,
        )
        assert target == {
            'url': 'https://r2.example/b/k?X-Amz-Signature=1',
            'headers': {'Content-Type': 'video/mp4'},
        }
        assert s3.size('uploads/k.mp4') == 42

    def test_a_missing_object(self):
        from botocore.exceptions import ClientError

        s3 = S3Storage(bucket='b')
        s3._client = mock.Mock()
        s3._client.head_object.side_effect = ClientError({'Error': {'Code': '404'}}, 'HeadObject')
        assert s3.size('nope') is None
        s3._client.head_object.side_effect = ClientError({'Error': {'Code': '403'}}, 'HeadObject')
        with pytest.raises(ClientError):
            s3.size('nope')
