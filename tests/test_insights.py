"""Teacher insights and notifiers."""

import datetime
import io
import json
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from tests.test_hardening import ROTATED
from tests.testapp.models import Lesson
from vidlock import insights, keys, notify
from vidlock.models import WatchProgress

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def lesson():
    # 25 s: buckets 0-10, 10-20, 20-25.
    return Lesson.objects.create(
        title='Lenz',
        video_key='l/a.ts',
        sealed_state='sealed',
        sealed_playlist=ROTATED,
        sealed_key=keys.wrap(bytes(48)),
    )


def watched(lesson, name, buckets, position, days_ago=0):
    user = User.objects.create_user(
        name, email=f'{name}@example.com', password='pw', first_name='+201000000000'
    )
    lesson.students.add(user)
    row = WatchProgress.objects.create(
        user=user,
        video_type='testapp.Lesson',
        video_id=str(lesson.pk),
        buckets=buckets,
        position=position,
        duration=25,
        seconds_watched=10,
    )
    WatchProgress.objects.filter(pk=row.pk).update(
        last_seen=timezone.now() - datetime.timedelta(days=days_ago)
    )
    return user


@pytest.fixture
def classroom(lesson):
    finished = [watched(lesson, f'done{i}', [1, 3, 1], 25) for i in range(3)]
    stalled = watched(lesson, 'stalled', [1, 1, 0], 15, days_ago=10)
    recent = watched(lesson, 'recent', [1, 0, 0], 12)
    waiting = User.objects.create_user('waiting', email='waiting@example.com', password='pw')
    lesson.students.add(waiting)
    User.objects.create_user('teacher', email='teacher@example.com', password='pw', is_staff=True)
    return finished, stalled, recent, waiting


def test_what_a_teacher_should_know(lesson, classroom):
    _finished, stalled, _recent, waiting = classroom
    found = insights.for_video(lesson)
    assert found['audience'] == 6 and found['started'] == 5 and found['finished'] == 3
    assert found['not_started'] == [waiting]
    assert found['stalled'] == [stalled]
    assert found['drop_off_at'] == 10, 'both who stopped, stopped in 10-20 s'
    assert found['confusing'] and found['confusing'][0][0] == 10, 'everyone replays 10-20 s'


def test_the_digest(lesson, classroom):
    text = insights.digest([lesson])
    assert '3 of 6 finished, 5 started, 1 not yet' in text
    assert '1 stopped and have not come back for 7 days' in text
    assert 'most who stop, stop around 0:10' in text and '0:10 is replayed' in text


def test_the_digest_in_arabic(lesson, classroom):
    from django.utils import translation

    with translation.override('ar'):
        assert 'بدأ' in insights.digest([lesson])


def test_send_by_email_and_nudge(lesson, classroom, settings):
    settings.VIDLOCK = {**settings.VIDLOCK, 'NOTIFIER': 'vidlock.notify.email'}
    out = io.StringIO()
    call_command('vidlock_insights', '--send', '--nudge', stdout=out)
    assert '1 digest(s) sent, 1 reminder(s) sent' in out.getvalue()
    assert sorted(m.to[0] for m in mail.outbox) == ['teacher@example.com', 'waiting@example.com']


def test_sending_needs_a_notifier():
    with pytest.raises(CommandError):
        call_command('vidlock_insights', '--send', stdout=io.StringIO())


class Answer:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_whatsapp_cloud_api(lesson, classroom, settings):
    settings.VIDLOCK = {
        **settings.VIDLOCK,
        'NOTIFIER': 'vidlock.notify.whatsapp',
        'WHATSAPP_TOKEN': 'tok',
        'WHATSAPP_PHONE_NUMBER_ID': '123',
        'WHATSAPP_TEMPLATE': 'lesson_report',
    }
    sent = []
    with mock.patch('urllib.request.urlopen', side_effect=lambda req, timeout: sent.append(req) or Answer()):
        assert notify.send([classroom[1]], 'Report', 'hello') == 1
    request = sent[0]
    assert request.full_url == 'https://graph.facebook.com/v21.0/123/messages'
    assert request.headers['Authorization'] == 'Bearer tok'
    body = json.loads(request.data)
    assert body['to'] == '201000000000' and body['template']['name'] == 'lesson_report'
    assert body['template']['language']['code'] == 'ar'


def test_webhook(lesson, classroom, settings):
    settings.VIDLOCK = {
        **settings.VIDLOCK,
        'NOTIFIER': 'vidlock.notify.webhook',
        'NOTIFY_WEBHOOK_URL': 'https://hooks.example/abc',
    }
    sent = []
    with mock.patch('urllib.request.urlopen', side_effect=lambda req, timeout: sent.append(req) or Answer()):
        assert notify.send(classroom[0], 'Report', 'hello') == 3
    assert json.loads(sent[0].data)['recipients'] == ['done0', 'done1', 'done2']


def test_without_a_notifier_nothing_is_sent(classroom):
    assert notify.send(classroom[0], 's', 'm') == 0
