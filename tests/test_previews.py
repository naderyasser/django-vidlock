"""Posters, thumbnails and seek previews (vidlock.previews), and who may see them."""

from unittest import mock
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from tests.testapp import storage
from tests.testapp.models import Lesson
from vidlock import previews, tokens
from vidlock.models import Preview
from vidlock.pipeline import seal
from vidlock.views import playback_info

pytestmark = pytest.mark.django_db

SOURCE = 'lessons/1/upload.mp4'


@pytest.fixture
def on(settings):
    settings.VIDLOCK = {**settings.VIDLOCK, 'THUMBNAILS': True}
    return settings


@pytest.fixture
def student():
    return get_user_model().objects.create_user('amira', password='pw')


@pytest.fixture
def sealed(on, sample, student):
    storage.OBJECTS[SOURCE] = sample
    lesson = Lesson.objects.create(title='Lenz', video_key=SOURCE, sealed_state='pending')
    lesson.students.add(student)
    assert seal(Lesson, lesson.pk, SOURCE) == 'sealed'
    lesson.refresh_from_db()
    return lesson


def jpeg(key):
    body, content_type = storage.OBJECTS[key]
    assert content_type == 'image/jpeg' and body[:2] == b'\xff\xd8'
    return body


def test_off_by_default(sample):
    storage.OBJECTS[SOURCE] = sample
    lesson = Lesson.objects.create(title='x', video_key=SOURCE)
    assert seal(Lesson, lesson.pk, SOURCE) == 'sealed'
    assert not Preview.objects.exists()
    assert not [k for k in storage.OBJECTS if k.endswith('.jpg')]


def test_sealing_makes_poster_thumbnail_and_sprites(sealed, tmp_path):
    found = previews.current(sealed)
    stem = sealed.video_key[: -len('.ts')]
    assert found.poster_key == f'{stem}.poster.jpg' and found.thumbnail_key == f'{stem}.thumb.jpg'
    assert found.sprites == [f'{stem}.sprite-0.jpg']
    for key in found.keys:
        jpeg(key)
    # The 25 s sample is 160x120: tiles every 10 s, 10x10 per sheet.
    assert (found.count, found.interval, found.columns) == (3, 10.0, 10)
    assert (found.tile_width, found.tile_height) == (160, 120)
    path = tmp_path / 'sheet.jpg'
    path.write_bytes(jpeg(found.sprites[0]))
    assert previews.jpeg_size(str(path)) == (1600, 1200)
    vtt = previews.webvtt(found, lambda key: f'https://cdn/{key}')
    assert vtt.splitlines()[:3] == ['WEBVTT', '', '00:00:00.000 --> 00:00:10.000']
    assert f'https://cdn/{found.sprites[0]}#xywh=320,0,160,120' in vtt
    assert '00:00:20.000 --> 00:00:25.000' in vtt


def test_resealing_replaces_the_old_previews(sealed, sample):
    old = previews.current(sealed).keys
    Lesson.objects.filter(pk=sealed.pk).update(video_key='lessons/1/second.mp4', sealed_state='pending')
    storage.OBJECTS['lessons/1/second.mp4'] = sample
    assert seal(Lesson, sealed.pk, 'lessons/1/second.mp4') == 'sealed'
    sealed.refresh_from_db()
    assert Preview.objects.count() == 1
    assert previews.current(sealed).keys != old
    assert set(old) <= set(storage.DELETED)


def test_a_preview_failure_never_fails_the_seal(on, sample):
    storage.OBJECTS[SOURCE] = sample
    lesson = Lesson.objects.create(title='x', video_key=SOURCE)
    with mock.patch('vidlock.previews.make', side_effect=previews.PreviewError('boom')):
        assert seal(Lesson, lesson.pk, SOURCE) == 'sealed'
    assert not Preview.objects.exists()


def test_a_stale_seal_leaves_no_preview_behind(on, sample):
    from vidlock import packager

    storage.OBJECTS[SOURCE] = sample
    lesson = Lesson.objects.create(title='x', video_key=SOURCE)
    real = packager.package

    def package_then_replace(*args):
        result = real(*args)
        Lesson.objects.filter(pk=lesson.pk).update(video_key='lessons/1/newer.mp4')
        return result

    with mock.patch('vidlock.packager.package', side_effect=package_then_replace):
        assert seal(Lesson, lesson.pk, SOURCE) == 'stale'
    assert not [k for k in storage.OBJECTS if k.endswith('.jpg')]


def test_a_reencoded_video_gets_previews_too(on, sample, tmp_path):
    from tests.test_transcode import make

    on.VIDLOCK = {**on.VIDLOCK, 'TRANSCODE': True, 'TRANSCODE_ENCODER': 'libx264'}
    source = make(tmp_path / 'a.mov', '-c:v', 'libx265', '-x265-params', 'log-level=none', seconds=12)
    storage.OBJECTS['v/a.mov'] = source
    lesson = Lesson.objects.create(title='x', video_key='v/a.mov')
    assert seal(Lesson, lesson.pk, 'v/a.mov') == 'sealed'
    lesson.refresh_from_db()
    found = previews.current(lesson)
    assert found is not None and found.count == 2 and (found.tile_width, found.tile_height) == (160, 90)


def test_previews_of_an_older_file_are_not_served(sealed):
    Lesson.objects.filter(pk=sealed.pk).update(video_key='lessons/1/other.ts')
    sealed.refresh_from_db()
    assert previews.current(sealed) is None


def test_forget_removes_them(sealed, django_capture_on_commit_callbacks):
    keys = previews.current(sealed).keys
    with django_capture_on_commit_callbacks(execute=True):
        previews.forget(sealed)
    assert not Preview.objects.exists() and set(keys) <= set(storage.DELETED)


def test_deleting_a_video_deletes_its_previews(sealed, django_capture_on_commit_callbacks):
    keys = previews.current(sealed).keys
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        sealed.delete()
        assert not set(keys) & set(storage.DELETED), 'storage waits for the commit'
    assert len(callbacks) == 1
    assert not Preview.objects.exists() and set(keys) <= set(storage.DELETED)


@pytest.mark.django_db(transaction=True)
def test_a_rolled_back_delete_keeps_the_images(on, sample, student):
    from django.db import transaction

    storage.OBJECTS[SOURCE] = sample
    lesson = Lesson.objects.create(title='x', video_key=SOURCE)
    assert seal(Lesson, lesson.pk, SOURCE) == 'sealed'
    lesson.refresh_from_db()
    keys = previews.current(lesson).keys
    with pytest.raises(RuntimeError), transaction.atomic():
        lesson.delete()
        raise RuntimeError('changed my mind')
    assert Preview.objects.count() == 1 and not set(keys) & set(storage.DELETED)
    assert all(key in storage.OBJECTS for key in keys)


def test_deleting_other_models_touches_nothing(sealed, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        get_user_model().objects.create_user('someone').delete()
    assert callbacks == [] and Preview.objects.count() == 1


def test_a_video_without_previews_costs_no_storage(settings, django_capture_on_commit_callbacks):
    lesson = Lesson.objects.create(title='x', video_key='v/a.mp4')
    with (
        # The patch must outlive the callbacks, which run as the capture exits.
        mock.patch('vidlock.storage.default_storage', side_effect=AssertionError('no storage')),
        django_capture_on_commit_callbacks(execute=True) as callbacks,
    ):
        lesson.delete()
    assert callbacks == []


def test_a_storage_failure_never_breaks_the_delete(sealed, django_capture_on_commit_callbacks, caplog):
    with (
        # The patch must outlive the callbacks, which run as the capture exits.
        mock.patch('vidlock.storage.default_storage', side_effect=RuntimeError('bucket down')),
        django_capture_on_commit_callbacks(execute=True),
    ):
        sealed.delete()
    assert not Lesson.objects.filter(pk=sealed.pk).exists()
    assert 'could not remove the previews' in caplog.text


# -- serving -------------------------------------------------------------------------


def info(lesson, user):
    request = RequestFactory().get('/')
    request.user = user
    return playback_info(request, lesson)


def test_playback_info_offers_poster_and_thumbnails(sealed, student):
    data = info(sealed, student)
    assert '/poster.jpg?t=' in data['poster_url'] and '/thumbnails.vtt?t=' in data['thumbnails_url']


def test_no_preview_urls_without_previews(settings, sample, student):
    storage.OBJECTS[SOURCE] = sample
    lesson = Lesson.objects.create(title='x', video_key=SOURCE)
    seal(Lesson, lesson.pk, SOURCE)
    lesson.refresh_from_db()
    assert 'poster_url' not in info(lesson, student) and 'thumbnails_url' not in info(lesson, student)


def path_of(url):
    parts = urlsplit(url)
    return parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}


def test_the_viewer_gets_short_lived_signed_urls(client, sealed, student):
    found = previews.current(sealed)
    for name, key in (('poster_url', found.poster_key), ('thumbnails_url', None)):
        path, query = path_of(info(sealed, student)[name])
        response = client.get(path, query)
        if key:
            assert response.status_code == 302
            assert response['Location'] == f'https://bucket.example/{key}?sig=1'
            assert response['Cache-Control'] == 'private, no-store'
            assert (key, 600) in storage.SIGNED
        else:
            assert response.status_code == 200 and response['Content-Type'].startswith('text/vtt')
            assert (
                f'https://bucket.example/{found.sprites[0]}?sig=1#xywh=0,0,160,120'
                in response.content.decode()
            )
    path, query = path_of(info(sealed, student)['poster_url'])
    thumb = client.get(path.replace('poster.jpg', 'thumbnail.jpg'), query)
    assert thumb['Location'] == f'https://bucket.example/{found.thumbnail_key}?sig=1'


@pytest.mark.parametrize('name', ['poster.jpg', 'thumbnail.jpg', 'thumbnails.vtt'])
def test_refused_like_the_playlist(client, sealed, student, name):
    url = f'/sealed/{sealed.pk}/{name}'
    assert client.get(url).status_code == 403, 'no token'
    assert client.get(url, {'t': 'forged'}).status_code == 403
    stranger = get_user_model().objects.create_user('stranger')
    assert client.get(url, {'t': tokens.sign(sealed.pk, stranger.pk, tokens.APP)}).status_code == 403
    other = Lesson.objects.create(title='y')
    assert client.get(url, {'t': tokens.sign(other.pk, student.pk, tokens.APP)}).status_code == 403
    t = tokens.sign(sealed.pk, student.pk, tokens.APP)
    assert client.get(url, {'t': t}).status_code in (200, 302)
    sealed.students.remove(student)  # enrolment revoked inside the token's life
    assert client.get(url, {'t': t}).status_code == 403


def test_expired_tokens_are_refused(client, sealed, student, settings):
    t = tokens.sign(sealed.pk, student.pk, tokens.APP)
    settings.VIDLOCK = {**settings.VIDLOCK, 'TOKEN_TTL': -1}
    assert client.get(f'/sealed/{sealed.pk}/poster.jpg', {'t': t}).status_code == 403


class TestPublicPoster:
    @pytest.fixture
    def public(self, sealed, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'POSTER_PUBLIC': True}
        return sealed

    def test_poster_and_thumbnail_need_no_token(self, client, public):
        found = previews.current(public)
        poster = client.get(f'/sealed/{public.pk}/poster.jpg')
        assert poster.status_code == 302 and poster['Location'].endswith(f'{found.poster_key}?sig=1')
        assert poster['Cache-Control'] == 'public, max-age=300'
        assert client.get(f'/sealed/{public.pk}/thumbnail.jpg').status_code == 302

    def test_sprites_still_need_the_viewer(self, client, public, student):
        assert client.get(f'/sealed/{public.pk}/thumbnails.vtt').status_code == 403
        t = tokens.sign(public.pk, student.pk, tokens.APP)
        assert client.get(f'/sealed/{public.pk}/thumbnails.vtt', {'t': t}).status_code == 200

    def test_the_backend_can_hide_a_poster(self, client, public):
        with mock.patch('tests.testapp.backend.LessonBackend.poster_visible', return_value=False):
            assert client.get(f'/sealed/{public.pk}/poster.jpg').status_code == 404

    def test_an_unsealed_video_has_none(self, client, public):
        Lesson.objects.filter(pk=public.pk).update(sealed_state='pending')
        assert client.get(f'/sealed/{public.pk}/poster.jpg').status_code == 404

    def test_urls_for_landing_pages(self, public, student):
        request = RequestFactory().get('/')
        assert previews.public_url(request, public) == f'http://testserver/sealed/{public.pk}/poster.jpg'
        assert previews.public_url(request, public, 'thumbnail').endswith('/thumbnail.jpg')
        assert info(public, student)['poster_url'] == f'http://testserver/sealed/{public.pk}/poster.jpg'

    def test_no_landing_url_when_not_public(self, sealed):
        assert previews.public_url(RequestFactory().get('/'), sealed) is None


def test_jpeg_size_rejects_other_files(tmp_path):
    path = tmp_path / 'x.jpg'
    path.write_bytes(b'not a jpeg at all, sorry')
    with pytest.raises(previews.PreviewError):
        previews.jpeg_size(str(path))


def test_previews_survive_a_missing_ffmpeg(on, tmp_path):
    with (
        mock.patch('vidlock.packager.ffmpeg_binary', return_value=None),
        pytest.raises(previews.PreviewError),
    ):
        previews.make('x.mp4', str(tmp_path), 10)
