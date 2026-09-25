import subprocess
from unittest import mock

import pytest

from tests.testapp import storage
from tests.testapp.models import Lesson
from vidlock.pipeline import seal, wants

pytestmark = pytest.mark.django_db

SOURCE = 'lessons/1/upload.mp4'


@pytest.fixture
def lesson(sample):
    storage.OBJECTS[SOURCE] = sample
    return Lesson.objects.create(title='Lenz', video_key=SOURCE, sealed_state='pending')


def test_the_sealed_copy_replaces_the_upload(lesson):
    assert seal(Lesson, lesson.pk, SOURCE) == 'sealed'

    lesson.refresh_from_db()
    assert lesson.is_sealed
    assert lesson.video_key.startswith('lessons/1/') and lesson.video_key.endswith('.ts')
    body, content_type = storage.OBJECTS[lesson.video_key]
    assert content_type == 'video/mp2t'
    assert lesson.video_size == len(body)
    assert storage.DELETED == [SOURCE], 'only the encrypted copy is kept'


def test_a_video_replaced_mid_job_throws_the_work_away(lesson):
    from vidlock import packager

    real = packager.package

    def package_then_replace(*args):
        result = real(*args)
        Lesson.objects.filter(pk=lesson.pk).update(video_key='lessons/1/newer.mp4')
        return result

    with mock.patch('vidlock.packager.package', side_effect=package_then_replace):
        assert seal(Lesson, lesson.pk, SOURCE) == 'stale'
    lesson.refresh_from_db()
    assert lesson.video_key == 'lessons/1/newer.mp4'
    assert not lesson.is_sealed
    assert not [k for k in storage.OBJECTS if k.endswith('.ts')], 'no orphan left in the bucket'
    assert SOURCE not in storage.DELETED


@pytest.mark.django_db
def test_a_stale_job_does_not_even_download():
    lesson = Lesson.objects.create(title='x', video_key='lessons/2/b.mp4')
    with mock.patch.object(storage.MemoryStorage, 'download') as download:
        assert seal(Lesson, lesson.pk, 'lessons/2/a.mp4') == 'stale'
    download.assert_not_called()


@pytest.mark.django_db
def test_hevc_is_left_as_uploaded(tmp_path):
    source = tmp_path / 'a.mov'
    source.write_bytes(b'not really a video')
    storage.OBJECTS['v/a.mov'] = str(source)
    lesson = Lesson.objects.create(title='x', video_key='v/a.mov')
    with (
        mock.patch('vidlock.packager.available', return_value=True),
        mock.patch('vidlock.packager.probe', return_value=('hevc', 'aac')),
    ):
        assert seal(Lesson, lesson.pk, 'v/a.mov') == 'skipped'
    lesson.refresh_from_db()
    assert lesson.video_key == 'v/a.mov' and 'hevc' in lesson.sealed_error
    assert storage.DELETED == []


@pytest.mark.django_db
def test_a_failure_keeps_the_upload_and_leaves_no_orphan(tmp_path):
    source = tmp_path / 'a.mp4'
    source.write_bytes(b'x')
    storage.OBJECTS['v/a.mp4'] = str(source)
    lesson = Lesson.objects.create(title='x', video_key='v/a.mp4')
    with (
        mock.patch('vidlock.packager.available', return_value=True),
        mock.patch('vidlock.packager.probe', return_value=('h264', 'aac')),
        mock.patch('vidlock.packager.package', side_effect=subprocess.CalledProcessError(1, 'ffmpeg')),
    ):
        assert seal(Lesson, lesson.pk, 'v/a.mp4') == 'failed'
    lesson.refresh_from_db()
    assert lesson.video_key == 'v/a.mp4' and lesson.sealed_state == 'failed'
    assert 'v/a.mp4' not in storage.DELETED


def test_wants_only_containers_that_hold_h264():
    with mock.patch('vidlock.packager.available', return_value=True):
        assert wants('x/y.MP4') and wants('x/y.mov')
        assert not wants('x/y.webm')
    with mock.patch('vidlock.packager.available', return_value=False):
        assert not wants('x/y.mp4')
