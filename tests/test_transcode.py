"""Re-encoding uploads that cannot be sealed as they are (vidlock.transcode)."""

import logging
import os
import subprocess
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from tests.conftest import HAS_FFMPEG
from tests.testapp import storage
from tests.testapp.models import Lesson
from vidlock import keys, packager, signals, transcode
from vidlock.models import Transcode
from vidlock.pipeline import seal, wants

pytestmark = pytest.mark.django_db

FFMPEG = os.environ.get('VIDLOCK_TEST_FFMPEG', 'ffmpeg')
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason='ffmpeg is not installed')

QUEUED = []
SEALS = []


def enqueue_transcode(model, pk, key):
    QUEUED.append((model, pk, key))


def enqueue_seal(model, pk, key):
    SEALS.append((model, pk, key))


@pytest.fixture(autouse=True)
def _fresh():
    QUEUED.clear()
    SEALS.clear()
    transcode.forget_detection()
    yield
    transcode.forget_detection()


@pytest.fixture
def on(settings):
    settings.VIDLOCK = {**settings.VIDLOCK, 'TRANSCODE': True, 'TRANSCODE_ENCODER': 'libx264'}
    return settings


def make(path, *video, size='320x180', seconds=3, audio=('-c:a', 'aac'), extra=()):
    subprocess.run(
        [
            FFMPEG, '-hide_banner', '-loglevel', 'error', '-y',
            '-f', 'lavfi', '-i', f'testsrc2=duration={seconds}:size={size}:rate=25',
            '-f', 'lavfi', '-i', f'sine=duration={seconds}',
            *video, *audio, *extra, '-shortest', str(path),
        ],
        check=True,
    )  # fmt: skip
    return str(path)


def upload(key, path):
    storage.OBJECTS[key] = path
    return Lesson.objects.create(title='x', video_key=key, sealed_state='pending')


def unsealed(lesson, tmp_path):
    """Decrypt the sealed video back to an MP4 and probe it."""
    lesson.refresh_from_db()
    ts = tmp_path / 'back.ts'
    ts.write_bytes(storage.OBJECTS[lesson.video_key][0])
    out = packager.unpackage(
        str(ts), lesson.sealed_playlist, keys.unwrap(lesson.sealed_key), str(tmp_path / 'b.mp4')
    )
    return packager.probe(out)


# -- off by default -------------------------------------------------------------


@needs_ffmpeg
def test_off_by_default_hevc_stays_skipped(tmp_path):
    lesson = upload('v/a.mov', make(tmp_path / 'a.mov', '-c:v', 'libx265', '-x265-params', 'log-level=none'))
    assert seal(Lesson, lesson.pk, 'v/a.mov') == 'skipped'
    lesson.refresh_from_db()
    assert lesson.video_key == 'v/a.mov' and 'hevc' in lesson.sealed_error
    assert not Transcode.objects.exists()
    assert storage.DELETED == []


def test_mkv_and_webm_are_only_wanted_with_transcode(settings):
    with mock.patch('vidlock.packager.available', return_value=True):
        assert not wants('x/y.mkv') and not wants('x/y.webm')
        settings.VIDLOCK = {**settings.VIDLOCK, 'TRANSCODE': True}
        assert wants('x/y.mkv') and wants('x/y.WEBM') and wants('x/y.mp4')


# -- re-encodes -------------------------------------------------------------------


@needs_ffmpeg
def test_iphone_hevc_10bit_hdr_is_reencoded_and_sealed(on, tmp_path):
    on.VIDLOCK = {**on.VIDLOCK, 'TRANSCODE_HEIGHT': 144}
    source = make(
        tmp_path / 'iphone.mov',
        '-c:v', 'libx265', '-pix_fmt', 'yuv420p10le', '-x265-params', 'log-level=none',
        '-color_primaries', 'bt2020', '-color_trc', 'arib-std-b67', '-colorspace', 'bt2020nc',
        size='640x360',
    )  # fmt: skip
    found = packager.probe(source)
    assert (found.video, found.pix_fmt, found.hdr) == ('hevc', 'yuv420p10le', True)
    finished = []
    signals.transcode_finished.connect(lambda **kw: finished.append(kw), weak=False, dispatch_uid='t')
    try:
        lesson = upload('lessons/1/iphone.mov', source)
        assert seal(Lesson, lesson.pk, 'lessons/1/iphone.mov') == 'sealed'
    finally:
        signals.transcode_finished.disconnect(dispatch_uid='t')

    back = unsealed(lesson, tmp_path)
    assert (back.video, back.audio, back.pix_fmt) == ('h264', 'aac', 'yuv420p')
    assert back.profile == 'High'
    assert (back.width, back.height) == (256, 144)
    assert lesson.video_key.endswith('.ts') and lesson.is_sealed
    assert storage.DELETED == ['lessons/1/iphone.mov']
    row = Transcode.for_video(lesson)
    assert (row.state, row.encoder, row.progress, row.error) == ('done', 'libx264', 1.0, '')
    assert row.source_info.startswith('hevc/aac yuv420p10le 640x360 HDR')
    assert row.started and row.finished
    assert finished[0]['state'] == 'sealed' and finished[0]['encoder'] == 'libx264'


@needs_ffmpeg
def test_h264_444_is_reencoded(on, tmp_path):
    lesson = upload('v/a.mp4', make(tmp_path / 'a.mp4', '-c:v', 'libx264', '-pix_fmt', 'yuv444p'))
    assert seal(Lesson, lesson.pk, 'v/a.mp4') == 'sealed'
    back = unsealed(lesson, tmp_path)
    assert (back.pix_fmt, back.profile, back.width, back.height) == ('yuv420p', 'High', 320, 180)


@needs_ffmpeg
def test_portrait_keeps_its_shorter_side_and_never_upscales(on, tmp_path):
    on.VIDLOCK = {**on.VIDLOCK, 'TRANSCODE_HEIGHT': 240}
    lesson = upload(
        'v/p.mov',
        make(tmp_path / 'p.mov', '-c:v', 'libx265', '-x265-params', 'log-level=none', size='360x640'),
    )
    assert seal(Lesson, lesson.pk, 'v/p.mov') == 'sealed'
    back = unsealed(lesson, tmp_path)
    assert (back.width, back.height) == (240, 426)

    on.VIDLOCK = {**on.VIDLOCK, 'TRANSCODE_HEIGHT': 720}
    small = upload('v/s.mov', make(tmp_path / 's.mov', '-c:v', 'libx265', '-x265-params', 'log-level=none'))
    assert seal(Lesson, small.pk, 'v/s.mov') == 'sealed'
    back = unsealed(small, tmp_path)
    assert (back.width, back.height) == (320, 180)


@needs_ffmpeg
def test_only_the_sound_is_reencoded_when_only_the_sound_is_wrong(on, tmp_path):
    source = make(
        tmp_path / 'o.mp4',
        '-c:v',
        'libx264',
        '-pix_fmt',
        'yuv420p',
        size='640x360',
        audio=('-c:a', 'libopus'),
    )
    lesson = upload('v/o.mp4', source)
    assert seal(Lesson, lesson.pk, 'v/o.mp4') == 'sealed'
    back = unsealed(lesson, tmp_path)
    assert (back.video, back.audio, back.width) == ('h264', 'aac', 640), 'picture copied, not scaled'
    assert Transcode.for_video(lesson).encoder == 'copy'


@needs_ffmpeg
def test_an_mkv_is_remuxed_into_a_sealable_mp4(on, tmp_path):
    lesson = upload('v/a.mkv', make(tmp_path / 'a.mkv', '-c:v', 'libx264', '-pix_fmt', 'yuv420p'))
    assert seal(Lesson, lesson.pk, 'v/a.mkv') == 'sealed'
    assert unsealed(lesson, tmp_path).video == 'h264'
    assert Transcode.for_video(lesson).encoder == 'copy'


def test_a_file_without_a_picture_is_still_skipped(on, tmp_path):
    (tmp_path / 'a.mov').write_bytes(b'x')
    lesson = upload('v/a.mov', str(tmp_path / 'a.mov'))
    with (
        mock.patch('vidlock.packager.available', return_value=True),
        mock.patch('vidlock.packager.probe', return_value=packager.Probe(video='', audio='aac')),
    ):
        assert seal(Lesson, lesson.pk, 'v/a.mov') == 'skipped'


# -- the queue -----------------------------------------------------------------------


@needs_ffmpeg
def test_with_a_transcode_queue_the_seal_job_hands_it_over(on, tmp_path):
    on.VIDLOCK = {
        **on.VIDLOCK,
        'ENQUEUE_TRANSCODE': 'tests.test_transcode.enqueue_transcode',
        'ENQUEUE_SEAL': 'tests.test_transcode.enqueue_seal',
    }
    lesson = upload('v/a.mov', make(tmp_path / 'a.mov', '-c:v', 'libx265', '-x265-params', 'log-level=none'))
    assert seal(Lesson, lesson.pk, 'v/a.mov') == transcode.QUEUED
    assert [(Lesson, lesson.pk, 'v/a.mov')] == QUEUED and SEALS == []
    lesson.refresh_from_db()
    assert lesson.sealed_state == 'pending' and lesson.video_key == 'v/a.mov'
    row = Transcode.for_video(lesson)
    assert row.state == 'queued' and row.source_info.startswith('hevc/aac')

    assert transcode.run(Lesson, lesson.pk, 'v/a.mov') == 'sealed'
    lesson.refresh_from_db()
    assert lesson.is_sealed and Transcode.for_video(lesson).state == 'done'


def test_while_it_waits_the_viewer_gets_what_any_unsealed_video_gets(on):
    """PENDING during a re-encode: the same signed MP4 as an unsealed video, nothing new."""
    from vidlock.views import playback_info

    user = get_user_model().objects.create_user('amira')
    lesson = Lesson.objects.create(title='x', video_key='v/a.mov', sealed_state='pending')
    transcode.record(Lesson, lesson.pk, 'v/a.mov', 'running', progress=0.4)
    plain = Lesson.objects.create(title='y', video_key='v/b.mp4', sealed_state='')
    request = RequestFactory().get('/')
    request.user = user
    during, before = playback_info(request, lesson), playback_info(request, plain)
    assert set(during) == set(before) == {'format', 'url', 'expires_in'}
    assert during['format'] == before['format'] == 'mp4'
    assert during['url'] == 'https://bucket.example/v/a.mov?sig=1'


@needs_ffmpeg
def test_a_newer_upload_during_the_encode_wins(on, tmp_path):
    lesson = upload('v/a.mov', make(tmp_path / 'a.mov', '-c:v', 'libx265', '-x265-params', 'log-level=none'))
    real = transcode.transcode_file

    def encode_then_replace(*args, **kwargs):
        used = real(*args, **kwargs)
        Lesson.objects.filter(pk=lesson.pk).update(video_key='v/newer.mp4')
        return used

    with mock.patch('vidlock.transcode.transcode_file', side_effect=encode_then_replace):
        assert transcode.run(Lesson, lesson.pk, 'v/a.mov') == 'stale'
    assert not [k for k in storage.OBJECTS if k.endswith('.ts')]
    assert 'v/a.mov' not in storage.DELETED
    assert Transcode.for_video(lesson).state == ''


def test_a_failed_encode_keeps_the_upload(on, tmp_path):
    (tmp_path / 'a.mov').write_bytes(b'x')
    lesson = upload('v/a.mov', str(tmp_path / 'a.mov'))
    finished = []
    signals.transcode_finished.connect(lambda **kw: finished.append(kw), weak=False, dispatch_uid='f')
    try:
        with (
            mock.patch('vidlock.packager.available', return_value=True),
            mock.patch('vidlock.packager.probe', return_value=packager.Probe('hevc', 'aac', duration=3)),
            mock.patch(
                'vidlock.transcode._run', side_effect=transcode.TranscodeError('ffmpeg exited 1: boom')
            ),
        ):
            assert transcode.run(Lesson, lesson.pk, 'v/a.mov') == 'failed'
    finally:
        signals.transcode_finished.disconnect(dispatch_uid='f')
    lesson.refresh_from_db()
    assert lesson.video_key == 'v/a.mov' and lesson.sealed_state == 'failed'
    assert lesson.sealed_error.startswith('re-encode: TranscodeError: ffmpeg exited 1')
    assert storage.DELETED == []
    row = Transcode.for_video(lesson)
    assert row.state == 'failed' and 'boom' in row.error
    assert finished[0]['state'] == 'failed'


def test_run_on_a_replaced_video_does_not_download(on):
    lesson = Lesson.objects.create(title='x', video_key='v/b.mov')
    with mock.patch.object(storage.MemoryStorage, 'download') as download:
        assert transcode.run(Lesson, lesson.pk, 'v/a.mov') == 'stale'
    download.assert_not_called()


# -- encoders ---------------------------------------------------------------------


@needs_ffmpeg
def test_auto_lands_on_libx264_quietly_without_a_gpu(settings, caplog):
    """Like production: nvenc/qsv/vaapi compiled into ffmpeg, no GPU."""
    settings.VIDLOCK = {**settings.VIDLOCK, 'TRANSCODE_VAAPI_DEVICE': '/nonexistent/renderD128'}
    caplog.set_level(logging.DEBUG, logger='vidlock')
    assert transcode.encoder() == 'libx264'
    assert not [r for r in caplog.records if r.levelno >= logging.INFO]
    with mock.patch('vidlock.transcode.subprocess.run') as run:
        assert transcode.encoder() == 'libx264'
    run.assert_not_called(), 'cached per process'


def test_auto_prefers_a_working_gpu(settings):
    with mock.patch('vidlock.transcode.works', side_effect=lambda name, binary=None: name == 'h264_qsv'):
        assert transcode.detect('ffmpeg') == 'h264_qsv'


def test_a_failing_gpu_falls_back_to_libx264(settings, caplog):
    settings.VIDLOCK = {**settings.VIDLOCK, 'TRANSCODE_ENCODER': 'h264_nvenc'}
    calls = []

    def fake_run(args, total, on_progress):
        name = args[args.index('-c:v') + 1]
        calls.append(name)
        if name == 'h264_nvenc':
            raise transcode.TranscodeError('ffmpeg exited 1: No NVENC capable devices found')

    with (
        mock.patch('vidlock.packager.ffmpeg_binary', return_value='ffmpeg'),
        mock.patch('vidlock.transcode._run', side_effect=fake_run),
    ):
        used = transcode.transcode_file('a.mov', 'out.mp4', packager.Probe('hevc', 'aac'))
    assert used == 'libx264' and calls == ['h264_nvenc', 'libx264']
    assert 'libx264 instead' in caplog.text


def test_commands_per_encoder(settings):
    settings.VIDLOCK = {**settings.VIDLOCK, 'TRANSCODE_CRF': 26, 'TRANSCODE_PRESET': 'faster'}
    hevc = packager.Probe('hevc', 'opus', 'yuv420p10le', duration=60)
    x264 = transcode.command('ffmpeg', 'in.mov', 'out.mp4', hevc, 'libx264')
    joined = ' '.join(x264)
    assert '-c:v libx264 -preset faster -crf 26 -profile:v high -pix_fmt yuv420p' in joined
    assert '-force_key_frames expr:gte(t,n_forced*10)' in joined
    assert '-c:a aac -b:a 128k' in joined and '-movflags +faststart' in joined
    assert '-map_metadata -1' in joined and '-f mov -protocol_whitelist file' in joined
    assert 'format=yuv420p' in joined and 'zscale' not in joined

    nvenc = ' '.join(transcode.command('ffmpeg', 'in.mov', 'o.mp4', hevc, 'h264_nvenc'))
    assert '-c:v h264_nvenc -preset p4 -rc vbr -cq 26' in nvenc
    qsv = ' '.join(transcode.command('ffmpeg', 'in.mov', 'o.mp4', hevc, 'h264_qsv'))
    assert '-c:v h264_qsv' in qsv and '-global_quality 26' in qsv and 'format=nv12' in qsv
    vaapi = transcode.command('ffmpeg', 'in.mov', 'o.mp4', hevc, 'h264_vaapi')
    assert vaapi.index('-vaapi_device') < vaapi.index('-i')
    assert 'format=nv12,hwupload' in ' '.join(vaapi) and '-qp' in vaapi

    mkv = transcode.command('ffmpeg', 'in.mkv', 'o.mp4', packager.Probe('vp9', 'opus'), 'libx264')
    assert mkv[mkv.index('-i') - 3 : mkv.index('-i')] == ['matroska', '-protocol_whitelist', 'file']


def test_hdr_is_tone_mapped_when_zscale_is_there():
    hdr = packager.Probe('hevc', 'aac', 'yuv420p10le', color_transfer='smpte2084')
    with mock.patch('vidlock.transcode.has_filter', return_value=True):
        assert 'tonemap=tonemap=hable' in ' '.join(transcode.command('ffmpeg', 'a.mov', 'o', hdr, 'libx264'))
    with mock.patch('vidlock.transcode.has_filter', return_value=False):
        assert 'tonemap' not in ' '.join(transcode.command('ffmpeg', 'a.mov', 'o', hdr, 'libx264'))


@needs_ffmpeg
def test_an_encode_past_the_timeout_is_killed(settings):
    settings.VIDLOCK = {**settings.VIDLOCK, 'TRANSCODE_TIMEOUT': 1}
    endless = [
        FFMPEG,
        '-hide_banner',
        '-loglevel',
        'error',
        '-re',
        '-f',
        'lavfi',
        '-i',
        'testsrc',
        '-f',
        'null',
        '-',
    ]
    with pytest.raises(transcode.TranscodeError, match='TRANSCODE_TIMEOUT'):
        transcode._run(endless, 0, None)


@needs_ffmpeg
def test_progress_is_reported(on, tmp_path):
    source = make(tmp_path / 'a.mov', '-c:v', 'libx265', '-x265-params', 'log-level=none', seconds=4)
    seen = []
    transcode.transcode_file(source, str(tmp_path / 'o.mp4'), packager.probe(source), seen.append)
    assert seen and seen[-1] == pytest.approx(1.0, abs=0.05)
    assert all(0 <= value <= 1 for value in seen)


def test_progress_writes_are_throttled(on):
    lesson = Lesson.objects.create(title='x', video_key='v/a.mov')
    transcode.record(Lesson, lesson.pk, 'v/a.mov', 'running')
    write = transcode._progress_writer(Lesson, lesson.pk, 'v/a.mov')
    with mock.patch('vidlock.transcode.time.monotonic', side_effect=[100.0, 100.5, 103.0]):
        write(0.10)
        write(0.20)  # half a second later: not written
        write(0.30)
    assert Transcode.for_video(lesson).progress == 0.30


# -- wrappers and checks ------------------------------------------------------------


def test_celery_sends_transcodes_to_their_own_queue_with_room_to_finish(settings):
    pytest.importorskip('celery')
    from vidlock.contrib import celery as contrib

    settings.VIDLOCK = {**settings.VIDLOCK, 'TRANSCODE_TIMEOUT': 3600, 'TRANSCODE_QUEUE': 'encodes'}
    with mock.patch.object(contrib.transcode_task, 'apply_async') as send:
        contrib.enqueue_transcode(Lesson, 7, 'v/a.mov')
    args, kwargs = send.call_args
    assert args == (('testapp.Lesson', 7, 'v/a.mov'),)
    assert kwargs['queue'] == 'encodes' and kwargs['time_limit'] > kwargs['soft_time_limit'] > 3600


@needs_ffmpeg
@pytest.mark.django_db(transaction=True)
def test_django_tasks_runs_transcodes_on_their_queue(on, tmp_path):
    pytest.importorskip('django.tasks')
    on.TASKS = {
        'default': {
            'BACKEND': 'django.tasks.backends.immediate.ImmediateBackend',
            'QUEUES': ['default', 'vidlock-transcode'],
        }
    }
    from vidlock.contrib import django_tasks as contrib

    lesson = upload('v/a.mov', make(tmp_path / 'a.mov', '-c:v', 'libx265', '-x265-params', 'log-level=none'))
    contrib.enqueue_transcode(Lesson, lesson.pk, 'v/a.mov')
    lesson.refresh_from_db()
    assert lesson.is_sealed


def test_checks(settings):
    from vidlock.checks import check_settings

    def ids():
        return {p.id: p for p in check_settings(None)}

    assert 'vidlock.W009' not in ids()
    settings.VIDLOCK = {**settings.VIDLOCK, 'TRANSCODE': True}
    warning = ids()['vidlock.W009']
    assert 'block the seal queue for hours' in warning.msg and 'TRANSCODE_TIMEOUT (14400 s)' in warning.msg
    assert 'concurrency 1' in warning.hint
    settings.VIDLOCK = {**settings.VIDLOCK, 'ENQUEUE_TRANSCODE': 'tests.test_transcode.enqueue_transcode'}
    assert 'vidlock.W009' not in ids()
    settings.VIDLOCK = {**settings.VIDLOCK, 'TRANSCODE_ENCODER': 'h265_magic'}
    assert 'vidlock.E007' in ids()
    settings.VIDLOCK = {**settings.VIDLOCK, 'TRANSCODE_ENCODER': 'auto', 'ENQUEUE_TRANSCODE': 'no.such.path'}
    assert 'vidlock.E006' in ids()


def test_check_without_libx264(settings):
    from vidlock.checks import check_settings

    settings.VIDLOCK = {**settings.VIDLOCK, 'TRANSCODE': True}
    with (
        mock.patch('vidlock.packager.ffmpeg_binary', return_value='/usr/bin/ffmpeg'),
        mock.patch('vidlock.transcode.compiled_encoders', return_value=frozenset({'aac'})),
    ):
        assert 'vidlock.W010' in {p.id for p in check_settings(None)}


@needs_ffmpeg
def test_doctor_reports_the_encoder(on, capsys):
    from django.core.management import call_command

    on.VIDLOCK = {**on.VIDLOCK, 'TRANSCODE_ENCODER': 'auto'}
    call_command('vidlock_doctor', '--offline')
    line = next(line for line in capsys.readouterr().out.splitlines() if ' transcode ' in line)
    assert 'libx264 (libx264 chosen; 2 s clip in' in line, line
