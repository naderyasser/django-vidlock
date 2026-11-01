"""A video that is not a model, behind a backend that is not a SealedBackend.

Projects that kept their own models (an adapter around their lesson exposing
vidlock's duck-typed contract) must get every request-path feature without a
Django ``_meta``: playback, playlist, key, heartbeat (with progress), captions,
previews and watermark tracing. ``video_type`` names the adapter's rows.
"""

import json
from urllib.parse import urlsplit

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from tests.test_views import PLAYLIST
from tests.testapp.models import Lesson
from vidlock import previews, tokens, trace
from vidlock.models import Preview, Transcript, WatchProgress
from vidlock.views import playback_info

pytestmark = pytest.mark.django_db

LABEL = 'videos.Lesson'


class SealedLesson:
    """The 0.3 contract, as a platform's own adapter exposes it: no _meta."""

    def __init__(self, lesson, video_type=LABEL):
        self._lesson = lesson
        if video_type is not None:
            self.video_type = video_type

    pk = property(lambda self: self._lesson.pk)
    video_key = property(lambda self: self._lesson.video_key)
    sealed_playlist = property(lambda self: self._lesson.sealed_playlist)
    sealed_key = property(lambda self: self._lesson.sealed_key)
    is_sealed = property(lambda self: self._lesson.is_sealed)
    sealed_duration = property(lambda self: self._lesson.sealed_duration)
    key_count = property(lambda self: self._lesson.key_count)

    def content_key(self, index=0):
        return self._lesson.content_key(index)

    def __str__(self):
        return str(self._lesson)


class DuckBackend:
    """Only the two required hooks; every optional one falls back to
    SealedBackend's default."""

    video_type = LABEL

    def get_video(self, video_id):
        lesson = Lesson.objects.filter(pk=video_id).first()
        return SealedLesson(lesson, self.video_type) if lesson else None

    def can_watch(self, user, video):
        return video._lesson.students.filter(pk=user.pk).exists()


class UnlabelledBackend(DuckBackend):
    video_type = None


class TracedBackend(DuckBackend):
    def watermark(self, user, video):
        return user.get_username()


@pytest.fixture
def duck(settings):
    settings.VIDLOCK = {**settings.VIDLOCK, 'BACKEND': 'tests.test_duck.DuckBackend'}
    return settings


@pytest.fixture
def student():
    return get_user_model().objects.create_user('student', password='pw')


@pytest.fixture
def lesson(student):
    lesson = Lesson.objects.create(
        title='Lenz',
        video_key='lessons/1/abc.ts',
        sealed_state='sealed',
        sealed_playlist=PLAYLIST,
        sealed_key=b'k' * 16,
    )
    lesson.students.add(student)
    return lesson


def info(video, user):
    request = RequestFactory().get('/')
    request.user, request.auth = user, object()
    return playback_info(request, video)


def get(client, url):
    split = urlsplit(url)
    return client.get(f'{split.path}?{split.query}')


def app_token(lesson, user):
    return tokens.sign(lesson.pk, user.pk, tokens.APP, tokens.account_fingerprint(user), '', 'L1')


def test_the_label(lesson):
    from vidlock.models import video_label

    assert video_label(lesson) == 'testapp.Lesson'
    assert video_label(SealedLesson(lesson)) == LABEL
    with pytest.raises(ImproperlyConfigured, match='video_type'):
        video_label(SealedLesson(lesson, video_type=None))


def test_playback_playlist_key_and_heartbeat(duck, client, lesson, student):
    data = info(SealedLesson(lesson), student)
    assert data['format'] == 'hls'
    assert 'captions_url' not in data and 'poster_url' not in data
    assert get(client, data['url']).status_code == 200
    assert get(client, data['key_url']).status_code == 200

    t = app_token(lesson, student)
    answer = client.post(
        f'/sealed/{lesson.pk}/heartbeat?t={t}',
        json.dumps({'position': 3, 'watched': [[0, 3]]}),
        content_type='application/json',
    )
    assert answer.status_code == 200
    assert WatchProgress.objects.filter(user=student, video_type=LABEL, video_id=str(lesson.pk)).exists()

    lesson.students.remove(student)
    assert get(client, data['url']).status_code == 403, 'can_watch() still decides'


def test_features_off_cost_nothing_and_need_no_label(settings, client, lesson, student):
    settings.VIDLOCK = {
        **settings.VIDLOCK,
        'BACKEND': 'tests.test_duck.UnlabelledBackend',
        'TRACK_PROGRESS': False,
    }
    with CaptureQueriesContext(connection) as queries:
        data = info(SealedLesson(lesson, video_type=None), student)
    assert data['format'] == 'hls'
    touched = ' '.join(q['sql'] for q in queries.captured_queries)
    assert Transcript._meta.db_table not in touched and Preview._meta.db_table not in touched
    assert get(client, data['url']).status_code == 200
    assert get(client, data['key_url']).status_code == 200


def test_captions(duck, settings, client, lesson, student):
    settings.VIDLOCK = {**settings.VIDLOCK, 'TRANSCRIBER': 'tests.test_transcribe.FakeTranscriber'}
    Transcript.objects.create(
        video_type=LABEL,
        video_id=str(lesson.pk),
        language='ar',
        segments=[{'start': 0, 'end': 2, 'text': 'hello'}],
        text='hello',
    )
    data = info(SealedLesson(lesson), student)
    assert data['captions_language'] == 'ar'
    vtt = get(client, data['captions_url'])
    assert vtt.status_code == 200 and 'hello' in vtt.content.decode()


def test_previews(duck, settings, client, lesson, student):
    settings.VIDLOCK = {**settings.VIDLOCK, 'THUMBNAILS': True}
    Preview.objects.create(
        video_type=LABEL,
        video_id=str(lesson.pk),
        video_key=lesson.video_key,
        poster_key='p/poster.jpg',
        thumbnail_key='p/thumb.jpg',
    )
    video = SealedLesson(lesson)
    data = info(video, student)
    assert get(client, data['poster_url']).status_code == 302
    assert get(client, data['thumbnails_url']).status_code == 200
    previews.forget(video)
    assert not Preview.objects.filter(video_type=LABEL).exists()


def test_the_watermark_traces_back(settings, lesson, student):
    settings.VIDLOCK = {**settings.VIDLOCK, 'BACKEND': 'tests.test_duck.TracedBackend'}
    data = info(SealedLesson(lesson), student)
    code = data['watermark'].rsplit(' · ', 1)[1]
    assert [m['user_id'] for m in trace.matches(code, [timezone.localdate()])] == [student.pk]


def test_the_doctor(duck, capsys):
    from django.core.management import call_command

    call_command('vidlock_doctor', '--offline')
    out = capsys.readouterr().out
    assert 'backend — DuckBackend (defaults: can_upload, namespace, watermark, poster_visible' in out


def test_optional_hooks_fall_back(student):
    from vidlock.backend import ask

    backend = DuckBackend()
    request = RequestFactory().get('/', REMOTE_ADDR='10.0.0.7')
    assert ask(backend, 'namespace', request) == '' == ask(None, 'namespace', request)
    assert ask(backend, 'client_ip', request) == '10.0.0.7'
    assert ask(backend, 'can_upload', student) is False
    assert ask(TracedBackend(), 'watermark', student, None) == 'student'
