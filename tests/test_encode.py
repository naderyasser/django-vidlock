"""vidlock.transcode.encode(): local file in, sealable MP4 out, nothing else.

No database here on purpose: encode() must not need a model, a task or a
storage (a test that touched the database would fail without the marker).
"""

import json
import os
import subprocess

import pytest

from tests.conftest import HAS_FFMPEG
from tests.test_transcode import FFMPEG, make
from vidlock import packager, transcode

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason='ffmpeg is not installed')
FFPROBE = packager.ffprobe_binary


@pytest.fixture(autouse=True)
def _fresh(settings):
    # TRANSCODE stays off: encode() does not depend on it.
    settings.VIDLOCK = {**settings.VIDLOCK, 'TRANSCODE': False}
    transcode.forget_detection()
    yield
    transcode.forget_detection()


def keyframes(path):
    run = subprocess.run(
        [FFPROBE(), '-v', 'error', '-select_streams', 'v:0', '-skip_frame', 'nokey', '-show_entries',
         'frame=pts_time', '-of', 'json', path],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return [round(float(f['pts_time']), 2) for f in json.loads(run.stdout)['frames']]


def test_an_iphone_hevc_hdr_file_is_reencoded_ready_to_package(tmp_path):
    source = make(
        tmp_path / 'IMG_0001.MOV',
        '-c:v', 'libx265', '-pix_fmt', 'yuv420p10le', '-x265-params', 'log-level=none',
        '-color_primaries', 'bt2020', '-color_trc', 'arib-std-b67', '-colorspace', 'bt2020nc',
        size='640x360', seconds=6,
    )  # fmt: skip
    seen = []
    out = str(tmp_path / 'out.mp4')
    result = transcode.encode(
        source, out, height=180, encoder='libx264', keyframe_seconds=2, on_progress=seen.append
    )

    assert result.output == out and os.path.isfile(out)
    assert (result.action, result.encoder, result.fell_back, result.reencoded) == (
        'reencoded',
        'libx264',
        False,
        True,
    )
    assert result.tonemapped is transcode.has_filter('zscale')
    assert result.source.video == 'hevc' and result.source.hdr
    assert (result.result.video, result.result.audio, result.result.pix_fmt) == ('h264', 'aac', 'yuv420p')
    assert result.result.profile == 'High' and (result.result.width, result.result.height) == (320, 180)
    assert not result.result.problem and result.seconds > 0
    assert keyframes(out)[:3] == [0.0, 2.0, 4.0]
    assert seen and seen[-1] == pytest.approx(1.0, abs=0.05)
    # Ready to package: vidlock's own sealing takes it as it is.
    ts_path, playlist, keys = packager.package(out, str(tmp_path / 'seal'))
    assert os.path.getsize(ts_path) > 0 and '#EXT-X-ENDLIST' in playlist and len(keys) % 16 == 0


def test_a_sealable_file_is_only_remuxed(tmp_path):
    source = make(tmp_path / 'a.mp4', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', size='640x360')
    result = transcode.encode(source, str(tmp_path / 'o.mp4'), height=180)
    assert (result.action, result.encoder, result.reencoded) == ('copied', 'copy', False)
    assert (result.result.width, result.result.height) == (640, 360), 'a kept picture is never scaled'


def test_only_the_sound_when_only_the_sound_is_wrong(tmp_path):
    source = make(tmp_path / 'a.mp4', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', audio=('-c:a', 'libopus'))
    result = transcode.encode(source, str(tmp_path / 'o.mp4'))
    assert (result.action, result.encoder) == ('audio', 'copy')
    assert (result.source.audio, result.result.audio) == ('opus', 'aac')


def test_keywords_beat_settings(settings, tmp_path):
    settings.VIDLOCK = {**settings.VIDLOCK, 'TRANSCODE_HEIGHT': 720, 'TRANSCODE_ENCODER': 'h264_nvenc'}
    source = make(tmp_path / 'a.mov', '-c:v', 'libx265', '-x265-params', 'log-level=none', size='640x360')
    result = transcode.encode(source, str(tmp_path / 'o.mp4'), height=144, encoder='libx264')
    assert result.encoder == 'libx264' and result.result.height == 144
    options = transcode.EncodeOptions.from_settings(crf=30)
    assert (options.height, options.encoder, options.crf) == (720, 'h264_nvenc', 30)


def test_a_forced_gpu_that_is_not_there_falls_back(tmp_path):
    if 'h264_nvenc' not in transcode.compiled_encoders():
        pytest.skip('this ffmpeg has no nvenc to fail with')
    source = make(tmp_path / 'a.mov', '-c:v', 'libx265', '-x265-params', 'log-level=none')
    result = transcode.encode(source, str(tmp_path / 'o.mp4'), encoder='h264_nvenc')
    if not result.fell_back:
        pytest.skip('this machine really has an NVIDIA GPU')
    assert result.encoder == 'libx264' and result.result.video == 'h264'


def test_timeout(tmp_path):
    source = make(
        tmp_path / 'a.mov', '-c:v', 'libx265', '-x265-params', 'log-level=none', size='1280x720', seconds=20
    )
    with pytest.raises(transcode.TranscodeTimeout):
        transcode.encode(source, str(tmp_path / 'o.mp4'), encoder='libx264', preset='veryslow', timeout=0.5)


def test_no_picture_no_ffmpeg(tmp_path, monkeypatch):
    audio_only = tmp_path / 'a.m4a'
    subprocess.run(
        [FFMPEG, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'sine=duration=2',
         '-c:a', 'aac', '-f', 'mp4', str(audio_only)],
        check=True,
    )  # fmt: skip
    with pytest.raises(transcode.TranscodeError, match='no picture'):
        transcode.encode(str(audio_only), str(tmp_path / 'o.mp4'))
    monkeypatch.setattr(packager, 'ffmpeg_binary', lambda: None)
    with pytest.raises(transcode.TranscodeError, match='not installed'):
        transcode.encode(str(audio_only), str(tmp_path / 'o.mp4'))
