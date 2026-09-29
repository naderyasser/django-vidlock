"""Profiles and monitor mode."""

import io
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.dispatch import receiver

from tests.test_hardening import ROTATED, app_token, get_key, info_for
from tests.testapp.models import Lesson
from vidlock import checks, conf, keys, monitor, risk, signals, tokens

pytestmark = pytest.mark.django_db


@pytest.fixture
def student():
    return get_user_model().objects.create_user('amira', password='pw')


@pytest.fixture
def lesson(student):
    lesson = Lesson.objects.create(
        title='Lenz',
        video_key='l/a.ts',
        sealed_state='sealed',
        sealed_playlist=ROTATED,
        sealed_key=keys.wrap(bytes(48)),
    )
    lesson.students.add(student)
    return lesson


class TestProfiles:
    def test_a_profile_sets_a_group(self, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'PROFILE': 'strict'}
        assert conf.get('MAX_STREAMS') == 1 and conf.get('REQUIRE_WRAPPED_KEY') is True
        assert conf.get('TOKEN_TTL') == 600, 'what the profile does not set keeps its default'

    def test_your_own_setting_wins(self, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'PROFILE': 'strict', 'MAX_STREAMS': 3}
        assert conf.get('MAX_STREAMS') == 3

    def test_balanced_is_the_defaults(self):
        assert conf.get('MAX_STREAMS') is None and conf.get('KEY_PACE') == 2.0

    def test_an_unknown_profile_is_an_error(self, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'PROFILE': 'paranoid'}
        assert 'vidlock.E005' in [p.id for p in checks.check_settings(None)]


class TestMonitorMode:
    @pytest.fixture(autouse=True)
    def monitoring(self, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'ENFORCE': False}

    def test_limits_count_instead_of_refusing(self, client, lesson, student, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'KEY_FETCHES_PER_HOUR': 1, 'KEY_BURST': 1}
        seen = []

        @receiver(signals.monitored, weak=False)
        def listen(sender, rule, user, **kwargs):
            seen.append((rule, user.pk))

        try:
            t = app_token(lesson, student)
            codes = [get_key(client, lesson, t, k).status_code for k in (0, 0, 1)]
        finally:
            signals.monitored.disconnect(listen)
        assert codes == [200, 200, 200]
        assert monitor.counts() == {'depth': 1, 'pace': 1}
        assert seen == [('depth', student.pk), ('pace', student.pk)]
        assert risk.score(student.pk) > 0, 'the risk score still adds up'

    def test_one_screen_is_only_counted(self, client, lesson, student, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'MAX_STREAMS': 1}
        first = info_for(student, lesson, device='a')
        info_for(student, lesson, device='b')
        t = first['key_url'].split('t=')[1]
        from urllib.parse import unquote

        assert get_key(client, lesson, unquote(t)).status_code == 200
        assert monitor.counts().get('elsewhere') == 1

    def test_a_flagged_viewer_is_not_paused(self, client, lesson, student, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'RISK_THRESHOLD': 1, 'RISK_SUSPEND_SECONDS': 600}
        risk.note(student, 'tamper')
        assert not risk.is_suspended(student.pk)
        assert monitor.counts().get('suspend') == 1

    def test_access_control_is_never_relaxed(self, client, lesson, student):
        # No token, a forged one, or a viewer who lost access: refused as always.
        assert get_key(client, lesson, '').status_code == 403
        assert get_key(client, lesson, 'forged').status_code == 403
        lesson.students.clear()
        assert get_key(client, lesson, app_token(lesson, student)).status_code == 403
        web = tokens.sign(lesson.pk, student.pk, tokens.WEB, tokens.account_fingerprint(student), 'sess')
        assert get_key(client, lesson, web).status_code == 403

    def test_status_and_checks_say_so(self, settings):
        monitor.would_refuse('pace')
        out = io.StringIO()
        call_command('vidlock_status', stdout=out)
        assert 'MONITOR MODE' in out.getvalue() and 'pace x1' in out.getvalue()
        assert 'vidlock.W008' in [p.id for p in checks.check_settings(None)]

    def test_enforcing_counts_nothing(self, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'ENFORCE': True}
        with mock.patch.object(signals.monitored, 'send') as send:
            assert monitor.would_refuse('pace') is True
        send.assert_not_called()
        assert monitor.counts() == {}
