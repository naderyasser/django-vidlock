"""Key rotation and pacing, the key exchange, stream leases, the risk score
and watermark codes."""

import base64
import datetime
import io
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.dispatch import receiver
from django.test import RequestFactory
from django.utils import timezone

from tests.testapp.models import Lesson
from vidlock import keys, packager, risk, signals, tokens, trace, wrap
from vidlock.playlist import KEY_PLACEHOLDER, key_count, render
from vidlock.views import playback_info

pytestmark = pytest.mark.django_db

# Three keys, one per 10 s period: a rotated 25-second video.
ROTATED = (
    '#EXTM3U\n'
    '#EXT-X-KEY:METHOD=AES-128,URI="__VIDLOCK_KEY__",IV=0x00\n#EXTINF:10.0,\n#EXT-X-BYTERANGE:16@0\n__VIDLOCK_MEDIA__\n'
    '#EXT-X-KEY:METHOD=AES-128,URI="__VIDLOCK_KEY__:1",IV=0x00\n#EXTINF:10.0,\n#EXT-X-BYTERANGE:16@16\n__VIDLOCK_MEDIA__\n'
    '#EXT-X-KEY:METHOD=AES-128,URI="__VIDLOCK_KEY__:2",IV=0x00\n#EXTINF:5.0,\n#EXT-X-BYTERANGE:16@32\n__VIDLOCK_MEDIA__\n'
    '#EXT-X-ENDLIST\n'
)
KEYS = bytes(range(48))


@pytest.fixture
def student():
    return get_user_model().objects.create_user('amira', password='pw')


@pytest.fixture
def rotated(student):
    lesson = Lesson.objects.create(
        title='Lenz',
        video_key='l/a.ts',
        sealed_state='sealed',
        sealed_playlist=ROTATED,
        sealed_key=keys.wrap(KEYS),
    )
    lesson.students.add(student)
    return lesson


def app_token(lesson, user, lease=''):
    return tokens.sign(lesson.pk, user.pk, tokens.APP, tokens.account_fingerprint(user), '', lease)


def get_key(client, lesson, t, k=None, **headers):
    params = {'t': t, **({'k': k} if k is not None else {})}
    return client.get(f'/sealed/{lesson.pk}/key', params, headers=headers)


def info_for(user, lesson, **get):
    request = RequestFactory().get('/', get)
    request.user, request.auth = user, object()
    request.META['HTTP_AUTHORIZATION'] = f'Bearer device-{get.get("device", "a")}'
    return playback_info(request, lesson)


def token_of(url):
    return parse_qs(urlsplit(url).query)['t'][0]


# -- rotation -------------------------------------------------------------


class TestRotation:
    def test_every_period_gets_its_own_key(self, sample, tmp_path, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'KEY_ROTATION_SECONDS': 10}
        ts, playlist, blob = packager.package(sample, str(tmp_path))
        assert len(blob) == 3 * 16 and key_count(playlist) == 3
        with open(ts, 'rb') as fh:
            data = fh.read()
        # Each range opens with its own period's key, and only with that one.
        ranges = [line for line in playlist.splitlines() if line.startswith('#EXT-X-BYTERANGE')]
        ivs = [line.rsplit('IV=0x', 1)[1] for line in playlist.splitlines() if line.startswith('#EXT-X-KEY')]
        for index, line in enumerate(ranges):
            length, offset = (int(n) for n in line.split(':')[1].split('@'))
            chunk = data[offset : offset + length]
            decryptor = Cipher(
                algorithms.AES(blob[index * 16 : index * 16 + 16]), modes.CBC(bytes.fromhex(ivs[index]))
            ).decryptor()
            clear = decryptor.update(chunk) + decryptor.finalize()
            assert clear[0] == 0x47 and clear[188] == 0x47

    def test_a_rotated_video_exports_back_to_mp4(self, sample, tmp_path, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'KEY_ROTATION_SECONDS': 10}
        work = tmp_path / 'w'
        work.mkdir()
        ts, playlist, blob = packager.package(sample, str(work))
        out = packager.unpackage(ts, playlist, blob, str(tmp_path / 'back.mp4'))
        assert tuple(packager.probe(out)) == ('h264', 'aac')

    def test_without_rotation_one_key(self, sample, tmp_path, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'KEY_ROTATION_SECONDS': None}
        _ts, playlist, blob = packager.package(sample, str(tmp_path))
        assert len(blob) == 16 and key_count(playlist) == 1

    def test_the_playlist_names_each_key(self):
        body = render(ROTATED, 'https://m', 'https://s/key?t=T')
        assert KEY_PLACEHOLDER not in body
        assert 'URI="https://s/key?t=T"' in body and 'URI="https://s/key?t=T&k=1"' in body
        assert 'URI="https://s/key?t=T&k=2"' in body

    def test_the_key_view_serves_each_key(self, client, rotated, student):
        t = app_token(rotated, student)
        assert get_key(client, rotated, t).content == KEYS[:16]
        assert get_key(client, rotated, t, 2).content == KEYS[32:48]
        assert get_key(client, rotated, t, 3).status_code == 404
        assert get_key(client, rotated, t, 'x').status_code == 404


class TestPacing:
    def test_keys_come_no_faster_than_playback(self, client, rotated, student, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'KEY_BURST': 2}
        t = app_token(rotated, student)
        assert get_key(client, rotated, t, 0).status_code == 200
        assert get_key(client, rotated, t, 1).status_code == 200
        refused = get_key(client, rotated, t, 2)
        assert refused.status_code == 429 and int(refused['Retry-After']) >= 1
        # Keys already given are free to fetch again.
        assert get_key(client, rotated, t, 0).status_code == 200

    def test_a_new_token_does_not_refill_the_bucket(self, client, rotated, student, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'KEY_BURST': 1}
        assert get_key(client, rotated, app_token(rotated, student), 0).status_code == 200
        assert get_key(client, rotated, app_token(rotated, student), 1).status_code == 429

    def test_the_bucket_refills_with_time(self, client, rotated, student, settings):
        from unittest import mock

        settings.VIDLOCK = {**settings.VIDLOCK, 'KEY_BURST': 1}
        t = app_token(rotated, student)
        assert get_key(client, rotated, t, 0).status_code == 200
        later = __import__('time').time() + 60
        with mock.patch('vidlock.guard.time.time', return_value=later):
            assert get_key(client, rotated, t, 1).status_code == 200

    def test_normal_viewing_fetches_each_key_many_times_without_tripping_depth(
        self, client, rotated, student, settings
    ):
        settings.VIDLOCK = {**settings.VIDLOCK, 'KEY_FETCHES_PER_HOUR': 3}
        t = app_token(rotated, student)
        for index in (0, 1, 2):
            for _ in range(3):
                assert get_key(client, rotated, t, index).status_code == 200


# -- key exchange -----------------------------------------------------------


def open_sealed(private, body):
    """What the bundled player does, in Python."""
    server = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), body[:65])
    shared = private.exchange(ec.ECDH(), server)
    key = HKDF(algorithm=hashes.SHA256(), length=16, salt=wrap.SALT, info=wrap.INFO).derive(shared)
    return AESGCM(key).decrypt(body[65:77], body[77:], None)


def share_of(private):
    raw = private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


class TestKeyExchange:
    def test_the_key_crosses_sealed_to_the_page(self, client, rotated, student):
        private = ec.generate_private_key(ec.SECP256R1())
        response = get_key(
            client, rotated, app_token(rotated, student), 1, x_vidlock_key_share=share_of(private)
        )
        assert response['Content-Type'] == wrap.CONTENT_TYPE
        assert KEYS[16:32] not in response.content
        assert open_sealed(private, response.content) == KEYS[16:32]

    def test_another_page_cannot_open_it(self, client, rotated, student):
        private = ec.generate_private_key(ec.SECP256R1())
        body = get_key(
            client, rotated, app_token(rotated, student), 0, x_vidlock_key_share=share_of(private)
        ).content
        with pytest.raises(InvalidTag):
            open_sealed(ec.generate_private_key(ec.SECP256R1()), body)

    def test_a_bad_share_is_a_400(self, client, rotated, student):
        assert (
            get_key(client, rotated, app_token(rotated, student), 0, x_vidlock_key_share='nope').status_code
            == 400
        )

    def test_raw_web_keys_can_be_refused(self, client, rotated, student, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'REQUIRE_WRAPPED_KEY': True}
        client.force_login(student)
        request = RequestFactory().get('/')
        request.user, request.session = student, client.session
        t = tokens.sign_for(request, rotated.pk, tokens.WEB)
        assert get_key(client, rotated, t).status_code == 403
        private = ec.generate_private_key(ec.SECP256R1())
        assert get_key(client, rotated, t, x_vidlock_key_share=share_of(private)).status_code == 200


# -- streams -----------------------------------------------------------------


class TestStreams:
    @pytest.fixture(autouse=True)
    def one_stream(self, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'MAX_STREAMS': 1}

    def test_a_second_device_takes_over(self, client, rotated, student):
        first = info_for(student, rotated, device='a')
        assert get_key(client, rotated, token_of(first['key_url'])).status_code == 200
        second = info_for(student, rotated, device='b')
        assert get_key(client, rotated, token_of(second['key_url'])).status_code == 200
        assert get_key(client, rotated, token_of(first['key_url'])).status_code == 409
        beat = client.post(
            first['heartbeat_url'].split('testserver')[1], '{}', content_type='application/json'
        )
        assert beat.status_code == 409 and beat.json()['error'] == 'elsewhere'
        assert risk.score(student.pk) >= risk.DEFAULT_WEIGHTS['takeover']

    def test_the_player_s_own_renewals_never_take_it_back(self, rotated, student):
        info_for(student, rotated, device='a')
        info_for(student, rotated, device='b')
        assert info_for(student, rotated, device='a', vidlock_resume='1')['format'] == 'elsewhere'
        # The holder's renewals go on.
        assert info_for(student, rotated, device='b', vidlock_resume='1')['format'] == 'hls'

    def test_a_lapsed_stream_comes_back_if_nobody_took_it(self, client, rotated, student):
        from unittest import mock

        first = info_for(student, rotated, device='a')
        # Past STREAM_TIMEOUT (90 s), inside the token's life (600 s).
        later = __import__('time').time() + 200
        with mock.patch('vidlock.streams.time.time', return_value=later):
            assert get_key(client, rotated, token_of(first['key_url'])).status_code == 200

    def test_heartbeats_keep_it(self, client, rotated, student):
        first = info_for(student, rotated, device='a')
        beat = client.post(
            first['heartbeat_url'].split('testserver')[1],
            json.dumps({'playing': True}),
            content_type='application/json',
        )
        assert beat.status_code == 200 and beat.json()['ok']

    def test_without_a_limit_anything_goes(self, client, rotated, student, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'MAX_STREAMS': None}
        first = info_for(student, rotated, device='a')
        info_for(student, rotated, device='b')
        assert get_key(client, rotated, token_of(first['key_url'])).status_code == 200


# -- risk ------------------------------------------------------------------------


class TestRisk:
    def test_crossing_the_threshold_flags_once_and_can_suspend(self, client, rotated, student, settings):
        settings.VIDLOCK = {
            **settings.VIDLOCK,
            'RISK_THRESHOLD': 4,
            'RISK_SUSPEND_SECONDS': 600,
            'RISK_WEIGHTS': {'tamper': 2},
        }
        seen = []

        @receiver(signals.viewer_flagged, weak=False)
        def listen(sender, user, score, events, suspended_for, **kwargs):
            seen.append((user.pk, score, events, suspended_for))

        try:
            for _ in range(3):
                risk.note(student, 'tamper')
        finally:
            signals.viewer_flagged.disconnect(listen)
        assert seen == [(student.pk, 4.0, {'tamper': 2}, 600)]
        assert get_key(client, rotated, app_token(rotated, student)).status_code == 403
        assert info_for(student, rotated)['format'] == 'blocked'
        out = io.StringIO()
        call_command('vidlock_risk', 'amira', stdout=out)
        assert 'SUSPENDED' in out.getvalue() and 'tamper x3' in out.getvalue()
        call_command('vidlock_risk', 'amira', '--clear', stdout=io.StringIO())
        assert get_key(client, rotated, app_token(rotated, student)).status_code == 200

    def test_a_key_that_is_never_played(self, client, rotated, student):
        from unittest import mock

        client.force_login(student)
        request = RequestFactory().get('/')
        request.user, request.session = student, client.session
        t = tokens.sign_for(request, rotated.pk, tokens.WEB, lease='L1')
        assert get_key(client, rotated, t).status_code == 200
        later = __import__('time').time() + risk.PLAYBACK_GRACE + 1
        with mock.patch('vidlock.risk.time.time', return_value=later):
            risk.sweep(student)
        state = risk.score(student.pk)
        assert state >= risk.DEFAULT_WEIGHTS['key_without_playback']

    def test_a_played_key_is_not_counted(self, client, rotated, student):
        from unittest import mock

        client.force_login(student)
        request = RequestFactory().get('/')
        request.user, request.session = student, client.session
        t = tokens.sign_for(request, rotated.pk, tokens.WEB, lease='L1')
        get_key(client, rotated, t, x_vidlock_key_share=share_of(ec.generate_private_key(ec.SECP256R1())))
        client.post(
            f'/sealed/{rotated.pk}/heartbeat?t={t}',
            json.dumps({'playing': True}),
            content_type='application/json',
        )
        later = __import__('time').time() + risk.PLAYBACK_GRACE + 1
        with mock.patch('vidlock.risk.time.time', return_value=later):
            risk.sweep(student)
        assert risk.score(student.pk) == 0

    def test_tamper_reports_count_once_per_stream(self, client, rotated, student):
        t = app_token(rotated, student, lease='L9')
        for _ in range(3):
            client.post(
                f'/sealed/{rotated.pk}/heartbeat?t={t}', '{"tamper": true}', content_type='application/json'
            )
        assert risk.score(student.pk) == risk.DEFAULT_WEIGHTS['tamper']

    def test_networks(self, student, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'MAX_NETWORKS_PER_DAY': 2}
        for ip in ('10.0.0.1', '10.0.0.200', '10.0.1.1', '10.0.2.1', '2001:db8::1'):
            risk.seen_from(student, ip)
        # 10.0.0.x is one /24: three networks past the first two... two over.
        assert risk.score(student.pk) == 2 * risk.DEFAULT_WEIGHTS['many_ips']

    def test_a_depth_refusal_counts(self, client, rotated, student, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'KEY_FETCHES_PER_HOUR': 1}
        t = app_token(rotated, student)
        get_key(client, rotated, t)
        assert get_key(client, rotated, t).status_code == 429
        assert risk.score(student.pk) == risk.DEFAULT_WEIGHTS['depth']


# -- watermark codes ------------------------------------------------------------


class TestTrace:
    def test_the_watermark_carries_a_code_that_leads_back(self, student, rotated, settings):
        from tests.testapp import backend

        backend.WATERMARK.append(True)
        try:
            text = info_for(student, rotated)['watermark']
        finally:
            backend.WATERMARK.clear()
        code = text.rsplit(' · ', 1)[1]
        assert code == trace.code_for(student) and len(code) == 6
        assert trace.find(code, [timezone.localdate()]) == [(student.pk, timezone.localdate())]
        out = io.StringIO()
        call_command('vidlock_trace', code.lower(), stdout=out)
        assert 'amira' in out.getvalue()

    def test_codes_change_every_day(self, student):
        today = timezone.localdate()
        assert trace.code_for(student, today) != trace.code_for(student, today - datetime.timedelta(days=1))

    def test_an_unknown_code(self, student):
        with pytest.raises(CommandError):
            call_command('vidlock_trace', 'AAAAAA', '--days', '3', stdout=io.StringIO())

    def test_codes_can_be_turned_off(self, student, rotated, settings):
        from tests.testapp import backend

        settings.VIDLOCK = {**settings.VIDLOCK, 'WATERMARK_CODE': False}
        backend.WATERMARK.append(True)
        try:
            assert info_for(student, rotated)['watermark'] == 'amira'
        finally:
            backend.WATERMARK.clear()


def test_playback_info_hands_the_player_its_heartbeat(student, rotated):
    info = info_for(student, rotated)
    assert info['heartbeat_url'].startswith(f'http://testserver/sealed/{rotated.pk}/heartbeat?t=')
    assert info['key_exchange'] == 'ecdh-p256-v1' and info['heartbeat_interval'] == 30
    assert tokens.claims(token_of(info['key_url']), rotated.pk).lease
