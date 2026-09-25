import time
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from vidlock import tokens
from vidlock.views import playback_info
from tests.testapp.backend import REPORTS
from tests.testapp.models import Lesson

pytestmark = pytest.mark.django_db

PLAYLIST = (
    '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="__VIDLOCK_KEY__",IV=0x00\n'
    '#EXTINF:10.0,\n#EXT-X-BYTERANGE:100@0\n__VIDLOCK_MEDIA__\n'
    '#EXTINF:5.0,\n#EXT-X-BYTERANGE:50@100\n__VIDLOCK_MEDIA__\n#EXT-X-ENDLIST\n'
)


@pytest.fixture
def people():
    User = get_user_model()
    return User.objects.create_user('student', password='pw'), User.objects.create_user(
        'stranger', password='pw'
    )


@pytest.fixture
def lesson(people):
    lesson = Lesson.objects.create(
        title='Lenz',
        video_key='lessons/1/abc.ts',
        sealed_state='sealed',
        sealed_playlist=PLAYLIST,
        sealed_key=b'k' * 16,
    )
    lesson.students.add(people[0])
    return lesson


def token(lesson, user, channel=tokens.APP):
    return tokens.sign(lesson.pk, user.pk, channel)


def key(client, lesson, t):
    return client.get(f'/sealed/{lesson.pk}/key', {'t': t})


def playlist(client, lesson, t):
    return client.get(f'/sealed/{lesson.pk}/media.m3u8', {'t': t})


def test_playback_info_for_a_sealed_video(lesson, people):
    request = RequestFactory().get('/')
    request.user = people[0]
    info = playback_info(request, lesson)
    assert info['format'] == 'hls'
    assert info['url'].startswith('http://testserver/sealed/') and '/media.m3u8?t=' in info['url']
    assert info['media_url'] == 'https://bucket.example/lessons/1/abc.ts?sig=1'
    t = parse_qs(urlsplit(info['key_url']).query)['t'][0]
    assert tokens.verify(t, lesson.pk) == (str(people[0].pk), tokens.WEB)


def test_a_token_authenticated_request_gets_an_app_token(lesson, people):
    request = RequestFactory().get('/')
    request.user, request.auth = people[0], object()
    t = parse_qs(urlsplit(playback_info(request, lesson)['key_url']).query)['t'][0]
    assert tokens.verify(t, lesson.pk)[1] == tokens.APP


def test_playback_info_before_sealing_is_the_plain_file(lesson, people):
    lesson.forget_seal()
    request = RequestFactory().get('/')
    request.user = people[0]
    assert playback_info(request, lesson) == {
        'format': 'mp4',
        'url': 'https://bucket.example/lessons/1/abc.ts?sig=1',
        'expires_in': 600,
    }


def test_the_playlist_is_rendered_for_this_viewer(client, lesson, people):
    response = playlist(client, lesson, token(lesson, people[0]))
    assert response.status_code == 200
    assert response['Content-Type'] == 'application/vnd.apple.mpegurl'
    assert 'no-store' in response['Cache-Control']
    body = response.content.decode()
    assert '__VIDLOCK_' not in body
    assert body.count('https://bucket.example/lessons/1/abc.ts?sig=1') == 2
    assert f'/sealed/{lesson.pk}/key?t=' in body


def test_the_key_goes_to_the_token_holder(client, lesson, people):
    response = key(client, lesson, token(lesson, people[0]))
    assert response.status_code == 200
    assert response.content == b'k' * 16


def test_no_token_no_key(client, lesson):
    assert key(client, lesson, '').status_code == 403
    assert playlist(client, lesson, 'forged:token').status_code == 403


def test_a_token_for_another_video_opens_nothing_here(client, lesson, people):
    other = Lesson.objects.create(title='other')
    assert key(client, lesson, tokens.sign(other.pk, people[0].pk, tokens.APP)).status_code == 403


def test_an_expired_token_is_refused(client, lesson, people):
    t = token(lesson, people[0])
    with mock.patch('django.core.signing.time.time', return_value=time.time() + 601):
        assert key(client, lesson, t).status_code == 403


def test_losing_access_inside_the_window_closes_the_door(client, lesson, people):
    t = token(lesson, people[0])
    lesson.students.clear()
    assert key(client, lesson, t).status_code == 403
    assert playlist(client, lesson, t).status_code == 403


def test_a_signed_token_for_someone_without_access_is_refused(client, lesson, people):
    assert key(client, lesson, token(lesson, people[1])).status_code == 403


def test_nothing_to_serve_before_sealing(client, lesson, people):
    lesson.forget_seal()
    lesson.save()
    assert key(client, lesson, token(lesson, people[0])).status_code == 404


class TestWebTokens:
    def test_without_its_cookie_the_key_is_refused(self, client, lesson, people):
        assert key(client, lesson, token(lesson, people[0], tokens.WEB)).status_code == 403

    def test_with_someone_elses_cookie_the_key_is_refused(self, client, lesson, people):
        lesson.students.add(people[1])
        client.force_login(people[1])
        assert key(client, lesson, token(lesson, people[0], tokens.WEB)).status_code == 403

    def test_with_its_own_cookie_the_key_is_served(self, client, lesson, people):
        client.force_login(people[0])
        assert key(client, lesson, token(lesson, people[0], tokens.WEB)).status_code == 200

    def test_the_playlist_needs_no_cookie(self, client, lesson, people):
        # Safari's native player may fetch it without one; the key is the gate.
        assert playlist(client, lesson, token(lesson, people[0], tokens.WEB)).status_code == 200


def test_a_burst_of_key_fetches_is_refused_and_reported_once(client, lesson, people, settings):
    settings.VIDLOCK = {**settings.VIDLOCK, 'KEY_FETCHES_PER_HOUR': 3}
    t = token(lesson, people[0])
    assert [key(client, lesson, t).status_code for _ in range(5)] == [200, 200, 200, 429, 429]
    assert REPORTS == [(people[0].pk, lesson.pk)]


def test_the_limit_is_per_video(client, lesson, people, settings):
    settings.VIDLOCK = {**settings.VIDLOCK, 'KEY_FETCHES_PER_HOUR': 3}
    other = Lesson.objects.create(
        title='other', video_key='x.ts', sealed_state='sealed', sealed_playlist=PLAYLIST, sealed_key=b'k' * 16
    )
    other.students.add(people[0])
    for _ in range(3):
        key(client, lesson, token(lesson, people[0]))
    assert key(client, other, token(other, people[0])).status_code == 200
