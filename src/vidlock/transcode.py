"""Re-encode an upload that cannot be sealed as it is, then seal it.

Opt-in (``VIDLOCK['TRANSCODE'] = True``). Without it, an HEVC, 10-bit,
VP9/AV1 or MKV/WebM upload is marked ``skipped`` and stays as uploaded,
unprotected. With it, ``vidlock.pipeline.seal`` hands such an upload here:

* the picture becomes H.264 High, 8-bit 4:2:0, its shorter side at most
  ``TRANSCODE_HEIGHT`` (never upscaled), a keyframe every
  ``SEGMENT_SECONDS`` so the ranges stay even, HDR tone-mapped to SDR when
  ffmpeg has zscale;
* the sound becomes AAC (copied when it already is);
* a picture that is already fine is copied, so a file whose only problem is
  its sound (Opus, AC-3) or its container (MKV) takes seconds;
* metadata (GPS from phones) is dropped; ``+faststart`` is set.

The MP4 exists only in the job's temporary folder: it is sealed right away
by the same code as any upload, so no clear H.264 copy reaches the bucket,
and any failure leaves the upload exactly as it was.

Heavy encodes should not hold up quick seals: point ``ENQUEUE_TRANSCODE`` at
a task on its own queue (``vidlock.contrib.celery.enqueue_transcode``), run
by a worker of its own with concurrency 1. Unset, the encode runs inside
the seal job that found it.

**Encoders.** ``TRANSCODE_ENCODER = 'auto'`` tries h264_nvenc, h264_qsv and
h264_vaapi with a one-frame test encode (an encoder being compiled into
ffmpeg does not mean the GPU is there) and settles on libx264 otherwise,
quietly: the answer is cached per process and logged at DEBUG. A hardware
encode that fails mid-job is retried once with libx264.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

from django.utils import timezone

from vidlock import conf, packager, signals

logger = logging.getLogger(__name__)

SOFTWARE = 'libx264'
#: In the order 'auto' tries them.
HARDWARE = ('h264_nvenc', 'h264_qsv', 'h264_vaapi')
ENCODERS = (SOFTWARE, *HARDWARE)

#: Returned by ``pipeline.seal`` when it handed the video to ENQUEUE_TRANSCODE.
QUEUED = 'transcoding'

#: How often progress is written to the database, at most.
PROGRESS_EVERY = 2.0


class TranscodeError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(conf.get('TRANSCODE'))


def needs(found: packager.Probe, path: str) -> bool:
    """Whether this source must go through a re-encode before sealing."""
    ext = os.path.splitext(path)[1].lower()
    return bool(found.problem) or ext in packager.TRANSCODE_EXTENSIONS


def can_fix(found: packager.Probe) -> bool:
    """A re-encode can only help a file that has a picture to begin with."""
    return bool(found.video)


# -- what this ffmpeg can do ---------------------------------------------------

_lock = threading.Lock()
_compiled: dict[str, frozenset] = {}
_filters: dict[str, frozenset] = {}
_working: dict[tuple, str] = {}


def _listing(binary: str, what: str) -> frozenset:
    try:
        run = subprocess.run(
            [binary, '-hide_banner', f'-{what}'], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    names = set()
    for line in run.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.fullmatch(r'[A-Z.]{3,6}', parts[0]):
            names.add(parts[1])
    return frozenset(names)


def compiled_encoders(binary: str | None = None) -> frozenset:
    """Encoder names this ffmpeg was built with (not necessarily usable)."""
    binary = binary or packager.ffmpeg_binary()
    if not binary:
        return frozenset()
    with _lock:
        if binary not in _compiled:
            _compiled[binary] = _listing(binary, 'encoders')
        return _compiled[binary]


def has_filter(name: str, binary: str | None = None) -> bool:
    binary = binary or packager.ffmpeg_binary()
    if not binary:
        return False
    with _lock:
        if binary not in _filters:
            _filters[binary] = _listing(binary, 'filters')
        return name in _filters[binary]


def _device() -> str:
    return conf.get('TRANSCODE_VAAPI_DEVICE') or '/dev/dri/renderD128'


def works(name: str, binary: str | None = None) -> bool:
    """Whether ``name`` really encodes here: one frame of a test pattern."""
    binary = binary or packager.ffmpeg_binary()
    if not binary or name not in compiled_encoders(binary):
        return False
    if name == 'h264_vaapi' and not os.path.exists(_device()):
        return False  # no render node: not worth a process
    command = [binary, '-hide_banner', '-loglevel', 'error', '-nostdin']
    if name == 'h264_vaapi':
        command += ['-vaapi_device', _device()]
    command += ['-f', 'lavfi', '-i', 'testsrc2=size=256x144:rate=25', '-frames:v', '1']
    if name == 'h264_vaapi':
        command += ['-vf', 'format=nv12,hwupload']
    elif name == 'h264_qsv':
        command += ['-pix_fmt', 'nv12']
    else:
        command += ['-pix_fmt', 'yuv420p']
    command += ['-c:v', name, '-f', 'null', '-']
    try:
        run = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return run.returncode == 0


def detect(binary: str | None = None) -> str:
    """The encoder ``'auto'`` settles on for this machine (cached)."""
    binary = binary or packager.ffmpeg_binary() or ''
    key = (binary, _device())
    with _lock:
        if key in _working:
            return _working[key]
    found = next((name for name in HARDWARE if works(name, binary)), SOFTWARE)
    logger.debug('vidlock: re-encoding with %s', found)
    with _lock:
        _working[key] = found
    return found


def forget_detection() -> None:
    """Drop what was learnt about this machine's ffmpeg (tests, a new GPU)."""
    with _lock:
        _compiled.clear()
        _filters.clear()
        _working.clear()


def encoder() -> str:
    choice = conf.get('TRANSCODE_ENCODER') or 'auto'
    if choice == 'auto':
        return detect()
    if choice not in ENCODERS:
        names = ', '.join(ENCODERS)
        raise TranscodeError(f"VIDLOCK['TRANSCODE_ENCODER'] = {choice!r} is not one of auto, {names}")
    return choice


# -- the ffmpeg command -------------------------------------------------------

#: Tone-map PQ/HLG to SDR BT.709 (needs ffmpeg built with zimg).
TONEMAP = (
    'zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,'
    'tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv'
)


def scale_filter(height: int) -> str:
    """Shorter side at most ``height``, never larger than the source, even
    dimensions. Applied after ffmpeg's autorotate, so portrait stays portrait."""
    factor = f'min(1\\,{int(height)}/min(iw\\,ih))'
    return f"scale=w='trunc(iw*{factor}/2)*2':h='trunc(ih*{factor}/2)*2'"


def command(binary: str, source: str, out: str, found: packager.Probe, name: str) -> list[str]:
    """The ffmpeg command that turns ``source`` into a sealable MP4 at ``out``."""
    crf = str(conf.get('TRANSCODE_CRF'))
    segment = conf.get('SEGMENT_SECONDS')
    args = [binary, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y']
    copy_video = found.video_ok
    if not copy_video and name == 'h264_vaapi':
        args += ['-vaapi_device', _device()]
    args += [*packager.input_args(source), '-i', source, '-map', '0:v:0', '-map', '0:a:0?']
    args += ['-map_metadata', '-1', '-map_chapters', '-1', '-sn', '-dn']
    if copy_video:
        args += ['-c:v', 'copy']
    else:
        # Scale first: tone-mapping works in 32-bit float per pixel, and at
        # 720p it has 2.25x fewer of them than at 1080p (1.6x faster overall).
        chain = [scale_filter(conf.get('TRANSCODE_HEIGHT'))]
        if found.hdr and has_filter('zscale', binary):
            chain.append(TONEMAP)
        if name == 'h264_vaapi':
            chain.append('format=nv12,hwupload')
        elif name == 'h264_qsv':
            chain.append('format=nv12')
        else:
            chain.append('format=yuv420p')
        args += ['-vf', ','.join(chain)]
        if name == SOFTWARE:
            args += ['-c:v', SOFTWARE, '-preset', str(conf.get('TRANSCODE_PRESET')), '-crf', crf]
            args += ['-profile:v', 'high', '-pix_fmt', 'yuv420p']
        elif name == 'h264_nvenc':
            args += ['-c:v', name, '-preset', 'p4', '-rc', 'vbr', '-cq', crf, '-b:v', '0']
            args += ['-profile:v', 'high', '-pix_fmt', 'yuv420p', '-forced-idr', '1']
        elif name == 'h264_qsv':
            args += ['-c:v', name, '-preset', 'veryfast', '-global_quality', crf, '-profile:v', 'high']
        elif name == 'h264_vaapi':
            args += ['-c:v', name, '-qp', crf, '-profile:v', 'high']
        args += ['-force_key_frames', f'expr:gte(t,n_forced*{segment})']
    if found.audio == 'aac':
        args += ['-c:a', 'copy']
    elif found.audio:
        args += ['-c:a', 'aac', '-b:a', str(conf.get('TRANSCODE_AUDIO_BITRATE')), '-ac', '2']
    args += ['-movflags', '+faststart', '-f', 'mp4', '-progress', 'pipe:1', '-nostats', out]
    return args


def _lowest_priority():
    os.nice(19)


def _run(args: list[str], total: float, on_progress) -> None:
    """Run ffmpeg, reporting the share done; kill it past TRANSCODE_TIMEOUT."""
    timeout = float(conf.get('TRANSCODE_TIMEOUT'))
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryFile(mode='w+') as errors:
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
            # os.nice only; the timer thread below starts after the fork.
            preexec_fn=_lowest_priority if hasattr(os, 'nice') else None,  # noqa: PLW1509
        )
        timer = threading.Timer(timeout, process.kill)
        timer.daemon = True
        timer.start()
        try:
            for line in process.stdout:
                key, _, value = line.strip().partition('=')
                if key == 'out_time_us' and total > 0 and on_progress:
                    with contextlib.suppress(ValueError):
                        on_progress(max(0.0, min(1.0, int(value) / 1e6 / total)))
            process.wait()
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait()
        if time.monotonic() >= deadline:
            raise TranscodeError(f'the re-encode took longer than TRANSCODE_TIMEOUT ({int(timeout)} s)')
        if process.returncode:
            errors.seek(0)
            raise TranscodeError(f'ffmpeg exited {process.returncode}: {errors.read().strip()[-300:]}')


def transcode_file(source: str, out: str, found: packager.Probe, on_progress=None) -> str:
    """Re-encode ``source`` into a sealable MP4 at ``out``. Returns the encoder
    that did it ('copy' when only the sound or the container changed)."""
    binary = packager.ffmpeg_binary()
    if not binary:
        raise TranscodeError('ffmpeg is not installed')
    if found.video_ok:
        _run(command(binary, source, out, found, SOFTWARE), found.duration, on_progress)
        return 'copy'
    name = encoder()
    try:
        _run(command(binary, source, out, found, name), found.duration, on_progress)
        return name
    except TranscodeError as exc:
        if name == SOFTWARE or 'TRANSCODE_TIMEOUT' in str(exc):
            raise
        logger.warning('vidlock: %s failed (%s); re-encoding with libx264 instead', name, exc)
    if on_progress:
        on_progress(0.0)
    _run(command(binary, source, out, found, SOFTWARE), found.duration, on_progress)
    return SOFTWARE


# -- the job -------------------------------------------------------------------


def _rows(model, pk):
    from vidlock.models import Transcode

    return Transcode.objects.filter(video_type=model._meta.label, video_id=str(pk))


def record(model, pk, source_key: str, state: str, **fields) -> None:
    """Create or update the video's Transcode row."""
    from vidlock.models import Transcode

    Transcode.objects.update_or_create(
        video_type=model._meta.label,
        video_id=str(pk),
        defaults={'source_key': source_key, 'state': state, **fields},
    )


def queue(model, pk, source_key: str, source_info: str = '') -> bool:
    """Hand a video to ``ENQUEUE_TRANSCODE``; False when it is not set."""
    enqueue = conf.load('ENQUEUE_TRANSCODE')
    if enqueue is None:
        return False
    model._default_manager.filter(pk=pk, video_key=source_key).update(
        sealed_state=model.STATE_PENDING, sealed_error=''
    )
    fields = {'progress': 0, 'error': '', 'encoder': '', 'started': None, 'finished': None}
    if source_info:
        fields['source_info'] = source_info[:200]
    record(model, pk, source_key, 'queued', **fields)
    enqueue(model, pk, source_key)
    return True


def _progress_writer(model, pk, source_key):
    last = {'at': 0.0, 'value': -1.0}

    def write(value: float) -> None:
        now = time.monotonic()
        if value not in (0.0, 1.0) and (now - last['at'] < PROGRESS_EVERY or value - last['value'] < 0.01):
            return
        last['at'], last['value'] = now, value
        _rows(model, pk).filter(source_key=source_key).update(progress=round(value, 4))

    return write


def encode_and_seal(model, pk, source_key, source_path, found, work, storage, delete_source) -> str:
    """Re-encode a source already on local disk, then seal it. Used by ``run``
    and, when no ENQUEUE_TRANSCODE is set, by ``pipeline.seal`` itself."""
    from vidlock import pipeline

    record(
        model, pk, source_key, 'running', progress=0, error='', encoder='',
        source_info=found.describe()[:200], started=timezone.now(), finished=None,
    )  # fmt: skip
    used = ''
    try:
        out = os.path.join(work, 'transcoded.mp4')
        used = transcode_file(source_path, out, found, _progress_writer(model, pk, source_key))
        result = packager.probe(out)
        if result.problem:
            raise TranscodeError(f'the re-encoded file still cannot be sealed: {result.problem}')
    except Exception as exc:
        logger.exception('re-encoding %s %s failed', model.__name__, pk)
        error = f'{type(exc).__name__}: {exc}'[:300]
        _rows(model, pk).filter(source_key=source_key).update(
            state='failed', error=error, encoder=used, finished=timezone.now()
        )
        state = pipeline._mark(model, pk, source_key, model.STATE_FAILED, f're-encode: {error}')
        signals.transcode_finished.send(
            sender=model, pk=pk, state=state, error=error, encoder=used, video_key=source_key
        )
        return state
    _rows(model, pk).filter(source_key=source_key).update(encoder=used, progress=1.0)
    state = pipeline.seal_local(model, pk, source_key, out, work, storage, delete_source)
    error = ''
    if state == model.STATE_SEALED:
        row_state = 'done'
    elif state == model.STATE_FAILED:
        row_state = 'failed'
        error = model._default_manager.filter(pk=pk).values_list('sealed_error', flat=True).first() or ''
    else:  # stale: a newer upload replaced this one while it was encoding
        row_state, error = '', 'replaced by a newer upload'
    _rows(model, pk).filter(source_key=source_key).update(
        state=row_state, error=error[:300], finished=timezone.now()
    )
    signals.transcode_finished.send(
        sender=model, pk=pk, state=state, error=error, encoder=used, video_key=source_key
    )
    return state


def run(model, pk, source_key: str, storage=None, delete_source: bool | None = None) -> str:
    """The job ``ENQUEUE_TRANSCODE`` queues: download, re-encode, seal.

    Synchronous and idempotent, like ``pipeline.seal``. Returns the video's
    resulting state: 'sealed', 'failed', 'skipped' or 'stale'.
    """
    from vidlock import pipeline
    from vidlock.storage import default_storage

    storage = storage or default_storage()
    if delete_source is None:
        delete_source = not conf.get('KEEP_SOURCE')
    row = model._default_manager.filter(pk=pk).values_list('video_key', flat=True).first()
    if row != source_key:
        _rows(model, pk).filter(source_key=source_key).update(state='', finished=timezone.now())
        return pipeline._finished(model, pk, 'stale', video_key=row or '')
    if not packager.available():
        record(model, pk, source_key, 'failed', error='ffmpeg is not installed')
        return pipeline._mark(model, pk, source_key, model.STATE_FAILED, 'ffmpeg is not installed')
    work = tempfile.mkdtemp(prefix='vidlock-transcode-')
    try:
        source = os.path.join(work, 'source' + os.path.splitext(source_key)[1].lower())
        try:
            storage.download(source_key, source)
            found = packager.probe(source)
        except Exception as exc:
            logger.exception('downloading %s %s for re-encoding failed', model.__name__, pk)
            error = f'{type(exc).__name__}: {exc}'[:300]
            record(model, pk, source_key, 'failed', error=error, finished=timezone.now())
            return pipeline._mark(model, pk, source_key, model.STATE_FAILED, error)
        if not needs(found, source):
            # Sealable after all (a retry of a video that failed for another reason).
            return pipeline.seal_local(model, pk, source_key, source, work, storage, delete_source)
        if not can_fix(found):
            problem = found.problem or 'no picture to re-encode'
            record(model, pk, source_key, '', error=problem[:300], source_info=found.describe()[:200])
            return pipeline._mark(model, pk, source_key, model.STATE_SKIPPED, problem)
        return encode_and_seal(model, pk, source_key, source, found, work, storage, delete_source)
    finally:
        shutil.rmtree(work, ignore_errors=True)
