"""Watch progress from the heartbeat."""

import io
import json
import time
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from tests.test_hardening import ROTATED
from tests.testapp.models import Lesson
from vidlock import keys, progress, tokens
from vidlock.models import WatchProgress

pytestmark = pytest.mark.django_db


@pytest.fixture
def student():
    return get_user_model().objects.create_user('amira', password='pw')


@pytest.fixture
def lesson(student):
    # ROTATED holds 25 s of video: three 10-second buckets.
    lesson = Lesson.objects.create(
        title='Lenz',
        video_key='l/a.ts',
        sealed_state='sealed',
        sealed_playlist=ROTATED,
        sealed_key=keys.wrap(bytes(48)),
    )
    lesson.students.add(student)
    return lesson


def beat(client, lesson, user, **body):
    t = tokens.sign(lesson.pk, user.pk, tokens.APP, tokens.account_fingerprint(user), '', 'L1')
    return client.post(
        f'/sealed/{lesson.pk}/heartbeat?t={t}', json.dumps(body), content_type='application/json'
    )


def row(user, lesson):
    return WatchProgress.objects.get(user=user, video_type='testapp.Lesson', video_id=str(lesson.pk))


def test_played_stretches_become_progress(client, lesson, student):
    assert beat(client, lesson, student, position=12, watched=[[0, 13.5]]).status_code == 200
    got = row(student, lesson)
    assert got.buckets == [1, 1] and got.seconds_watched == pytest.approx(13.5)
    # A bucket needs 3 s of a stretch: 20.0-22.5 is not enough to count.
    with mock.patch('vidlock.progress.time.time', return_value=time.time() + 60):
        beat(client, lesson, student, watched=[[20, 22.5]])
    assert row(student, lesson).buckets == [1, 1]
    assert got.position == 12 and got.duration == 25 and got.completion == pytest.approx(2 / 3)


def test_replays_are_counted_for_the_heatmap(client, lesson, student):
    beat(client, lesson, student, watched=[[0, 25]])
    with mock.patch('vidlock.progress.time.time', return_value=time.time() + 60):
        beat(client, lesson, student, watched=[[10, 20], [10, 20]])
    assert row(student, lesson).buckets == [1, 3, 1]
    report = progress.report(lesson)
    assert report['heatmap'] == [1, 3, 1] and report['most_replayed_at'] == 10
    assert report['completed'] == 1 and report['viewers'] == 1


def test_a_heartbeat_cannot_claim_more_than_could_have_played(client, lesson, student):
    beat(client, lesson, student, watched=[[0, 25]])
    # Straight away: only the slack is allowed, not another hour.
    beat(client, lesson, student, watched=[[0, 3600]] * 5)
    assert row(student, lesson).seconds_watched <= 25 + progress.SLACK + 1


def test_nonsense_is_ignored(client, lesson, student):
    beat(client, lesson, student, watched=[['a', 'b'], [5, 2], [-3, 4], 'x', [1]], position='nope')
    assert not WatchProgress.objects.exists()


def test_it_can_be_turned_off(client, lesson, student, settings):
    settings.VIDLOCK = {**settings.VIDLOCK, 'TRACK_PROGRESS': False}
    beat(client, lesson, student, watched=[[0, 10]])
    assert not WatchProgress.objects.exists()


def test_the_command_and_the_class_view(client, lesson, student):
    other = get_user_model().objects.create_user('omar', password='pw')
    lesson.students.add(other)
    beat(client, lesson, student, watched=[[0, 25]])
    beat(client, lesson, other, watched=[[0, 8]])
    report = progress.report(lesson)
    assert report['viewers'] == 2 and report['completed'] == 1
    assert report['average_completion'] == pytest.approx((1 + 1 / 3) / 2, abs=0.001)
    out = io.StringIO()
    call_command('vidlock_progress', 'testapp.Lesson', str(lesson.pk), stdout=out)
    assert '2 viewers, 1 finished' in out.getvalue() and 'most played around 0:00' in out.getvalue()
    assert [r.user for r in progress.for_viewer(student)] == [student]


def test_the_admin_lists_it(admin_client, client, lesson, student):
    beat(client, lesson, student, watched=[[0, 25]])
    page = admin_client.get('/admin/vidlock/watchprogress/')
    assert page.status_code == 200 and '100%' in page.content.decode()


def test_a_play_cut_into_pieces_by_heartbeats_still_counts(client, lesson, student):
    # Heartbeats every 2 s cut 0-12 s into pieces shorter than a bucket needs.
    now = time.time()
    for n, (start, stop) in enumerate([(0, 2), (2, 4), (4, 6), (6, 8), (8, 10), (10, 12), (12, 13.5)]):
        with mock.patch('vidlock.progress.time.time', return_value=now + 2 * n):
            beat(client, lesson, student, watched=[[start, stop]])
    got = row(student, lesson)
    assert got.buckets == [1, 1], 'each bucket once, not once per piece'
    # Seeking back and watching again is a replay.
    with mock.patch('vidlock.progress.time.time', return_value=now + 60):
        beat(client, lesson, student, watched=[[0, 9]])
    assert row(student, lesson).buckets == [2, 1]
