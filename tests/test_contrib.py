"""seal_later, the Celery and Django Tasks wrappers, and shared sessions."""

import time
from unittest import mock

import pytest
from django.contrib.auth import get_user_model

from tests.testapp import storage
from tests.testapp.models import Lesson
from vidlock import risk, tokens
from vidlock.pipeline import seal_later

pytestmark = pytest.mark.django_db(transaction=True)

QUEUED = []


def enqueue(model, pk, key):
    QUEUED.append((model, pk, key))


class TestSealLater:
    def test_it_records_the_upload_and_queues_after_commit(self, settings):
        settings.VIDLOCK = {**settings.VIDLOCK, 'ENQUEUE_SEAL': 'tests.test_contrib.enqueue'}
        QUEUED.clear()
        lesson = Lesson.objects.create(
            title='x', sealed_state='sealed', sealed_playlist='old', sealed_key=b'k' * 16
        )
        with mock.patch('vidlock.packager.available', return_value=True):
            assert seal_later(lesson, 'l/new.mp4')
        lesson.refresh_from_db()
        assert (lesson.video_key, lesson.sealed_state, lesson.sealed_playlist) == ('l/new.mp4', 'pending', '')
        assert [(Lesson, lesson.pk, 'l/new.mp4')] == QUEUED

    def test_it_seals_inline_without_a_queue(self, sample):
        storage.OBJECTS['l/a.mp4'] = sample
        lesson = Lesson.objects.create(title='x')
        assert seal_later(lesson, 'l/a.mp4')
        lesson.refresh_from_db()
        assert lesson.is_sealed

    def test_an_upload_it_cannot_seal(self):
        lesson = Lesson.objects.create(title='x')
        assert not seal_later(lesson, 'l/a.webm')
        lesson.refresh_from_db()
        assert lesson.video_key == 'l/a.webm' and lesson.sealed_state == ''


def test_celery_wrapper(sample):
    celery = pytest.importorskip('celery')
    app = celery.Celery('t', set_as_current=True)
    app.conf.task_always_eager = True
    from vidlock.contrib import celery as contrib

    storage.OBJECTS['l/a.mp4'] = sample
    lesson = Lesson.objects.create(title='x', video_key='l/a.mp4')
    contrib.enqueue(Lesson, lesson.pk, 'l/a.mp4')
    lesson.refresh_from_db()
    assert lesson.is_sealed


def test_django_tasks_wrapper(sample, settings):
    pytest.importorskip('django.tasks')
    settings.TASKS = {'default': {'BACKEND': 'django.tasks.backends.immediate.ImmediateBackend'}}
    from vidlock.contrib import django_tasks as contrib

    storage.OBJECTS['l/a.mp4'] = sample
    lesson = Lesson.objects.create(title='x', video_key='l/a.mp4')
    contrib.enqueue(Lesson, lesson.pk, 'l/a.mp4')
    lesson.refresh_from_db()
    assert lesson.is_sealed


class TestSharedSession:
    @pytest.fixture
    def student(self):
        return get_user_model().objects.create_user('amira', password='pw')

    def beat(self, client, student, ip, lease='L1'):
        t = tokens.sign(7, student.pk, tokens.APP, tokens.account_fingerprint(student), '', lease)
        return client.post(
            f'/sealed/7/heartbeat?t={t}', '{}', content_type='application/json', REMOTE_ADDR=ip
        )

    def test_one_stream_on_two_networks_at_once(self, client, student):
        self.beat(client, student, '10.0.0.1')
        self.beat(client, student, '192.168.5.9')
        self.beat(client, student, '10.0.0.1')
        assert risk.score(student.pk) == risk.DEFAULT_WEIGHTS['shared_session']

    def test_a_phone_that_moves_networks_is_not_sharing(self, client, student):
        self.beat(client, student, '10.0.0.1')
        later = time.time() + risk.CONCURRENT_WINDOW + 5
        with mock.patch('vidlock.risk.time.time', return_value=later):
            self.beat(client, student, '192.168.5.9')
        assert risk.score(student.pk) == 0

    def test_same_network_is_fine(self, client, student):
        for last in (1, 2, 3):
            self.beat(client, student, f'10.0.0.{last}')
        assert risk.score(student.pk) == 0
