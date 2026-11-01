"""The admin's re-encode column and action, and manage.py vidlock_transcode."""

from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command

from tests.conftest import HAS_FFMPEG
from tests.test_transcode import make
from tests.testapp import storage
from tests.testapp.models import Lesson
from vidlock import packager, transcode
from vidlock.models import Transcode

pytestmark = pytest.mark.django_db

needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason='ffmpeg is not installed')
QUEUED = []


def enqueue_transcode(model, pk, key):
    QUEUED.append((model, pk, key))


@pytest.fixture(autouse=True)
def _fresh():
    QUEUED.clear()


@pytest.fixture
def staff(client):
    user = get_user_model().objects.create_superuser('admin', 'a@example.com', 'pw')
    client.force_login(user)
    return client


@pytest.fixture
def on(settings):
    settings.VIDLOCK = {**settings.VIDLOCK, 'TRANSCODE': True, 'TRANSCODE_ENCODER': 'libx264'}
    return settings


def run(*args):
    from io import StringIO

    out = StringIO()
    call_command('vidlock_transcode', *args, stdout=out)
    return out.getvalue()


# -- admin -------------------------------------------------------------------------


def test_the_column_shows_progress_without_a_query_per_row(staff, django_assert_max_num_queries):
    for i in range(5):
        lesson = Lesson.objects.create(title=f'l{i}', video_key=f'v/{i}.mov', sealed_state='pending')
        transcode.record(Lesson, lesson.pk, lesson.video_key, 'running', progress=0.42, encoder='libx264')
    queued = Lesson.objects.create(title='q', video_key='v/q.mov', sealed_state='pending')
    transcode.record(Lesson, queued.pk, 'v/q.mov', 'queued')
    with django_assert_max_num_queries(12):
        page = staff.get('/admin/testapp/lesson/').content.decode()
    assert page.count('Re-encoding 42% · libx264') == 5
    assert 'Waiting to re-encode' in page


def test_the_change_page_shows_the_reencode(staff):
    lesson = Lesson.objects.create(title='x', video_key='v/a.mov', sealed_state='failed')
    transcode.record(
        Lesson,
        lesson.pk,
        'v/a.mov',
        'failed',
        encoder='libx264',
        source_info='hevc/aac 3840x2160',
        error='boom',
    )
    page = staff.get(f'/admin/testapp/lesson/{lesson.pk}/change/').content.decode()
    assert 'Failed · libx264 · from hevc/aac 3840x2160 · boom' in page


def action(staff, *lessons):
    return staff.post(
        '/admin/testapp/lesson/',
        {'action': 'transcode_again', '_selected_action': [lesson.pk for lesson in lessons]},
        follow=True,
    ).content.decode()


def test_the_action_needs_transcode_on(staff):
    lesson = Lesson.objects.create(title='x', video_key='v/a.mov', sealed_state='skipped')
    assert 'Turn on' in action(staff, lesson)
    assert not Transcode.objects.exists()


def test_the_action_queues_skipped_and_failed_videos(staff, on):
    on.VIDLOCK = {**on.VIDLOCK, 'ENQUEUE_TRANSCODE': 'tests.test_transcode_admin.enqueue_transcode'}
    skipped = Lesson.objects.create(title='a', video_key='v/a.mov', sealed_state='skipped')
    failed = Lesson.objects.create(title='b', video_key='v/b.mkv', sealed_state='failed')
    sealed = Lesson.objects.create(
        title='c', video_key='v/c.ts', sealed_state='sealed', sealed_playlist='x', sealed_key=b'k' * 16
    )
    busy = Lesson.objects.create(title='d', video_key='v/d.mov', sealed_state='pending')
    with mock.patch('vidlock.packager.available', return_value=True):
        page = action(staff, skipped, failed, sealed, busy)
    assert '2 videos sent for re-encoding.' in page
    assert sorted(pk for _, pk, _ in QUEUED) == [skipped.pk, failed.pk]
    skipped.refresh_from_db()
    assert skipped.sealed_state == 'pending' and Transcode.for_video(skipped).state == 'queued'


@needs_ffmpeg
def test_the_action_reencodes_inline_without_a_queue(staff, on, tmp_path):
    storage.OBJECTS['v/a.mov'] = make(tmp_path / 'a.mov', '-c:v', 'libx265', '-x265-params', 'log-level=none')
    lesson = Lesson.objects.create(title='x', video_key='v/a.mov', sealed_state='skipped')
    assert '1 video sent for re-encoding.' in action(staff, lesson)
    lesson.refresh_from_db()
    assert lesson.is_sealed


# -- the command --------------------------------------------------------------------


@pytest.fixture
def backlog(tmp_path):
    hevc = make(tmp_path / 'a.mov', '-c:v', 'libx265', '-x265-params', 'log-level=none', size='640x360')
    opus = make(tmp_path / 'b.mp4', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', audio=('-c:a', 'libopus'))
    storage.OBJECTS['v/a.mov'] = hevc
    storage.OBJECTS['v/b.mp4'] = opus
    a = Lesson.objects.create(
        title='a', video_key='v/a.mov', sealed_state='skipped', sealed_error='hevc/aac x'
    )
    b = Lesson.objects.create(title='b', video_key='v/b.mp4', sealed_state='skipped')
    Lesson.objects.create(title='c', video_key='v/c.mov', sealed_state='failed')
    return a, b


@needs_ffmpeg
def test_dry_run_sizes_the_backlog(backlog):
    a, b = backlog
    # The fake bucket has no HTTP side: the command falls back to downloading.
    with mock.patch('vidlock.packager.probe_url', side_effect=packager.PackagingError('no http')):
        out = run('--dry-run')
    lines = out.splitlines()
    first = next(line for line in lines if line.startswith(f'testapp.Lesson {a.pk} '))
    assert 'hevc/aac yuv420p  640x360  0:00:03' in first and 're-encode: hevc/aac' in first
    assert ' KB  ' in first
    second = next(line for line in lines if line.startswith(f'testapp.Lesson {b.pk} '))
    assert 'h264/opus yuv420p  320x180' in second and 'sound/container only, picture copied' in second
    assert lines[-1].startswith('2 video(s), 2 to re-encode; 0:00:06 of video (0.0 h), ')
    assert not Transcode.objects.exists(), 'a dry run changes nothing'
    assert Lesson.objects.get(pk=a.pk).sealed_state == 'skipped'


@needs_ffmpeg
def test_dry_run_probes_through_a_signed_url_when_it_can(backlog, live_server, settings):
    """Real HTTP: ffprobe reads the object through the (fake) bucket's Range-serving view."""
    a, _ = backlog
    with open(storage.OBJECTS['v/a.mov'], 'rb') as fh:
        storage.OBJECTS['v/a.mov'] = (fh.read(), 'video/quicktime')
    url = f'{live_server.url}/bucket/v/a.mov'
    with (
        mock.patch.object(storage.MemoryStorage, 'signed_url', return_value=url),
        mock.patch.object(storage.MemoryStorage, 'download', side_effect=AssertionError('no download')),
    ):
        out = run('--dry-run', '--pk', str(a.pk))
    assert 'hevc/aac yuv420p  640x360  0:00:03' in out


def test_dry_run_without_probing(backlog):
    with mock.patch.object(storage.MemoryStorage, 'download', side_effect=AssertionError('no download')):
        out = run('--dry-run', '--no-probe', '--state', 'skipped', '--state', 'failed')
    assert 'hevc/aac x' in out and 'not probed' in out
    assert out.splitlines()[-1].startswith('3 video(s), 0 to re-encode')


def test_a_real_run_needs_transcode():
    with pytest.raises(CommandError, match='TRANSCODE'):
        run()


def test_queue_needs_enqueue_transcode(on):
    with pytest.raises(CommandError, match='ENQUEUE_TRANSCODE'):
        run('--queue')


@needs_ffmpeg
def test_it_reencodes_the_skipped_videos(backlog, on):
    out = run()
    assert out.splitlines()[-1] == '2 sealed'
    assert all(lesson.is_sealed for lesson in Lesson.objects.filter(sealed_state='sealed'))


def test_it_queues_them(backlog, on):
    on.VIDLOCK = {**on.VIDLOCK, 'ENQUEUE_TRANSCODE': 'tests.test_transcode_admin.enqueue_transcode'}
    with mock.patch('vidlock.packager.available', return_value=True):
        out = run('--queue', '--limit', '1')
    assert out.splitlines()[-1] == '1 queued' and len(QUEUED) == 1
