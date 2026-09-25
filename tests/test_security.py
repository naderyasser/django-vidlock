"""Keys at rest, token binding, fetch limits and Fetch Metadata."""

from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.auth import get_user_model
from django.dispatch import receiver
from django.test import RequestFactory

from tests.test_views import PLAYLIST, key, playlist
from tests.testapp import storage
from tests.testapp.models import Lesson
from vidlock import guard, keys, signals, tokens
from vidlock.views import media_ttl, playback_info

pytestmark = pytest.mark.django_db

CONTENT_KEY = bytes(range(16))


@pytest.fixture
def student():
    return get_user_model().objects.create_user('student', password='pw')


def sealed(student, title='Lenz', raw=False):
    lesson = Lesson.objects.create(
        title=title,
        video_key=f'lessons/{title}.ts',
        sealed_state='sealed',
        sealed_playlist=PLAYLIST,
        sealed_key=CONTENT_KEY if raw else keys.wrap(CONTENT_KEY),
    )
    lesson.students.add(student)
    return lesson


def web_token(client, lesson):
    """A token issued the way playback_info issues it, to this client's session."""
    request = RequestFactory().get('/')
    request.user = lesson.students.get()
    request.session = client.session
    return tokens.sign_for(request, lesson.pk, tokens.WEB)


class TestKeysAtRest:
    def test_a_wrapped_key_is_not_the_key(self):
        blob = keys.wrap(CONTENT_KEY)
        assert CONTENT_KEY not in blob and keys.is_wrapped(blob)
        assert keys.unwrap(blob) == CONTENT_KEY
        assert keys.wrap(CONTENT_KEY) != blob, 'a fresh nonce every time'

    def test_a_raw_key_from_0_1_still_opens(self):
        assert keys.unwrap(CONTENT_KEY) == CONTENT_KEY
        assert keys.unwrap(b'VLK1' + bytes(12)) == b'VLK1' + bytes(12)
        assert keys.needs_rewrap(CONTENT_KEY)

    def test_a_tampered_blob_is_refused(self):
        blob = bytearray(keys.wrap(CONTENT_KEY))
        blob[30] ^= 1
        with pytest.raises(keys.KeyUnwrapError):
            keys.unwrap(bytes(blob))

    def test_rotation(self, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'KEY_ENCRYPTION_KEYS': ['old']}
        blob = keys.wrap(CONTENT_KEY)
        settings.VIDLOCK = {**settings.VIDLOCK, 'KEY_ENCRYPTION_KEYS': ['new', 'old']}
        assert keys.unwrap(blob) == CONTENT_KEY and keys.needs_rewrap(blob)
        assert not keys.needs_rewrap(keys.wrap(CONTENT_KEY))
        settings.VIDLOCK = {**settings.VIDLOCK, 'KEY_ENCRYPTION_KEYS': ['new']}
        with pytest.raises(keys.KeyUnwrapError):
            keys.unwrap(blob)

    def test_secret_key_fallbacks_open_old_keys(self, settings):
        blob = keys.wrap(CONTENT_KEY)
        settings.SECRET_KEY, settings.SECRET_KEY_FALLBACKS = 'rotated', [settings.SECRET_KEY]
        assert keys.unwrap(blob) == CONTENT_KEY

    def test_the_key_view_serves_the_clear_key(self, client, student):
        lesson = sealed(student)
        assert key(client, lesson, tokens.sign(lesson.pk, student.pk, tokens.APP)).content == CONTENT_KEY
        assert lesson.content_key() == CONTENT_KEY

    def test_a_key_no_secret_opens_is_a_503_not_garbage(self, client, student, settings):
        lesson = sealed(student)
        settings.VIDLOCK = {**settings.VIDLOCK, 'KEY_ENCRYPTION_KEYS': ['something-else']}
        assert key(client, lesson, tokens.sign(lesson.pk, student.pk, tokens.APP)).status_code == 503


class TestTokenBinding:
    def test_playback_info_binds_web_tokens_to_the_session(self, client, student):
        lesson = sealed(student)
        client.force_login(student)
        request = RequestFactory().get('/')
        request.user, request.session = student, client.session
        info = playback_info(request, lesson)
        found = tokens.claims(parse_qs(urlsplit(info['key_url']).query)['t'][0], lesson.pk)
        assert found.session and found.account

    def test_logging_out_ends_the_session_s_tokens(self, client, student):
        lesson = sealed(student)
        client.force_login(student)
        t = web_token(client, lesson)
        assert key(client, lesson, t).status_code == 200
        client.logout()
        client.force_login(student)  # same viewer, new session
        assert key(client, lesson, t).status_code == 403

    def test_a_password_change_voids_app_tokens(self, client, student):
        lesson = sealed(student)
        request = RequestFactory().get('/')
        request.user, request.auth = student, object()
        t = tokens.sign_for(request, lesson.pk)
        assert playlist(client, lesson, t).status_code == 200
        student.set_password('new')
        student.save()
        assert playlist(client, lesson, t).status_code == 403
        assert key(client, lesson, t).status_code == 403

    def test_an_unbound_0_1_token_still_works(self, client, student):
        lesson = sealed(student)
        assert key(client, lesson, tokens.sign(lesson.pk, student.pk, tokens.APP)).status_code == 200


class TestFetchMetadata:
    def fetch(self, client, lesson, **headers):
        return client.get(f'/sealed/{lesson.pk}/key', {'t': web_token(client, lesson)}, headers=headers)

    def test_a_same_origin_fetch_gets_the_key(self, client, student):
        lesson = sealed(student)
        client.force_login(student)
        response = self.fetch(client, lesson, sec_fetch_site='same-origin', sec_fetch_mode='cors')
        assert response.status_code == 200

    def test_cross_site_and_navigation_are_refused(self, client, student):
        lesson = sealed(student)
        client.force_login(student)
        assert self.fetch(client, lesson, sec_fetch_site='cross-site').status_code == 403
        assert self.fetch(client, lesson, sec_fetch_site='none', sec_fetch_mode='navigate').status_code == 403

    def test_strict_mode_wants_the_headers(self, client, student, settings):
        lesson = sealed(student)
        client.force_login(student)
        assert self.fetch(client, lesson).status_code == 200
        settings.VIDLOCK = {**settings.VIDLOCK, 'STRICT_FETCH_METADATA': True}
        assert self.fetch(client, lesson).status_code == 403
        assert self.fetch(client, lesson, sec_fetch_site='same-origin').status_code == 200

    def test_app_tokens_are_not_asked_for_them(self, client, student, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'STRICT_FETCH_METADATA': True}
        lesson = sealed(student)
        assert key(client, lesson, tokens.sign(lesson.pk, student.pk, tokens.APP)).status_code == 200


class TestHarvesting:
    def test_many_different_videos_in_an_hour_is_a_harvest(self, client, student, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'KEY_VIDEOS_PER_HOUR': 2}
        seen = []

        @receiver(signals.key_abuse, weak=False)
        def listen(sender, reason, user, video, **kwargs):
            seen.append((reason, user.pk))

        try:
            lessons = [sealed(student, f'l{i}') for i in range(4)]
            codes = [
                key(client, lesson, tokens.sign(lesson.pk, student.pk, tokens.APP)).status_code
                for lesson in lessons
            ]
        finally:
            signals.key_abuse.disconnect(listen)
        assert codes == [200, 200, 429, 429]
        assert seen == [(guard.BREADTH, student.pk)], 'reported once, not once per video'
        # Rewatching a video already keyed this hour is refused too, until the hour ends.
        again = key(client, lessons[0], tokens.sign(lessons[0].pk, student.pk, tokens.APP))
        assert again.status_code == 429

    def test_rewatching_one_video_does_not_count_as_breadth(self, client, student, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'KEY_VIDEOS_PER_HOUR': 1}
        lesson = sealed(student)
        t = tokens.sign(lesson.pk, student.pk, tokens.APP)
        assert [key(client, lesson, t).status_code for _ in range(3)] == [200, 200, 200]

    def test_breadth_can_be_turned_off(self, client, student, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'KEY_VIDEOS_PER_HOUR': None}
        for i in range(40):
            lesson = sealed(student, f'l{i}')
            assert key(client, lesson, tokens.sign(lesson.pk, student.pk, tokens.APP)).status_code == 200


class TestNativePlayers:
    def test_the_playlist_media_url_outlives_the_video(self, client, student):
        lesson = sealed(student)
        playlist(client, lesson, tokens.sign(lesson.pk, student.pk, tokens.APP))
        # PLAYLIST holds 15 s of media; the URL lives that plus a token's TTL.
        assert storage.SIGNED[-1] == (lesson.video_key, 615)

    def test_it_is_capped_at_what_s3_allows(self, student):
        lesson = sealed(student)
        lesson.sealed_playlist = '#EXTINF:900000.0,\n'
        assert media_ttl(lesson) == 7 * 24 * 3600

    def test_playback_info_reports_the_duration(self, student):
        lesson = sealed(student)
        request = RequestFactory().get('/')
        request.user = student
        assert playback_info(request, lesson)['duration'] == 15.0
