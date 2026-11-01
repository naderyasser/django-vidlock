"""Poster, thumbnail and seek-preview sprites for a sealed video.

Opt-in (``VIDLOCK['THUMBNAILS'] = True``). Made from the clear source in the
same job that seals it (or re-encodes it), uploaded through your storage
next to the .ts under the same random name, and recorded in the ``Preview``
table. A failure here is logged and never fails the seal.

* ``poster``: the most representative frame (ffmpeg's ``thumbnail``
  filter) of the second after ``THUMBNAIL_OFFSET``, shorter side at most
  720 px;
* ``thumbnail``: the same frame, 320 px wide;
* sprites: one tile every ``SPRITE_INTERVAL`` seconds, ``SPRITE_WIDTH``
  wide, ``SPRITE_COLUMNS`` x ``SPRITE_COLUMNS`` tiles per JPEG sheet, and a
  WebVTT track (``#xywh=`` fragments) that seek bars read. Tiles are cut
  from keyframes only, which keeps an hour of video to seconds of work;
  re-encoded videos have one every ``SEGMENT_SECONDS``.

They show the lesson, so they are served like it: ``vidlock:poster``,
``vidlock:thumbnail`` and ``vidlock:thumbnails`` check the viewer's token
and ``can_watch()``, then redirect to a short-lived signed URL. With
``POSTER_PUBLIC = True`` the poster and thumbnail are served without a
token (for course pages shown before purchase), as long as
``SealedBackend.poster_visible(video)`` agrees; the sprites never are.
"""

from __future__ import annotations

import glob
import logging
import math
import os
import subprocess

from vidlock import conf, packager

logger = logging.getLogger(__name__)

JPEG = 'image/jpeg'
TIMEOUT_SECONDS = 30 * 60
POSTER_SIZE = 720
THUMBNAIL_WIDTH = 320


class PreviewError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(conf.get('THUMBNAILS'))


def _ffmpeg(args: list[str]) -> None:
    binary = packager.ffmpeg_binary()
    if not binary:
        raise PreviewError('ffmpeg is not installed')
    run = subprocess.run(
        [binary, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', *args],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )
    if run.returncode:
        raise PreviewError(f'ffmpeg exited {run.returncode}: {run.stderr.strip()[-300:]}')


def jpeg_size(path: str) -> tuple[int, int]:
    """(width, height) from a JPEG's frame header."""
    with open(path, 'rb') as fh:
        data = fh.read()
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(data[i + 7 : i + 9], 'big'), int.from_bytes(data[i + 5 : i + 7], 'big')
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        i += 2 + int.from_bytes(data[i + 2 : i + 4], 'big')
    raise PreviewError(f'{os.path.basename(path)} is not a JPEG')


def make(source: str, folder: str, duration: float) -> dict:
    """Write the poster, thumbnail and sprite sheets for ``source`` into
    ``folder``. Returns local paths and the sprite layout."""
    from vidlock.transcode import scale_filter

    os.makedirs(folder, exist_ok=True)
    inp = packager.input_args(source)
    offset = max(0.0, min(float(conf.get('THUMBNAIL_OFFSET') or 0), duration / 2))
    poster = os.path.join(folder, 'poster.jpg')
    thumbnail = os.path.join(folder, 'thumbnail.jpg')
    _ffmpeg(
        [
            '-ss', f'{offset:.3f}', *inp, '-i', source,
            '-filter_complex',
            f'[0:v:0]thumbnail=25,split=2[a][b];[a]{scale_filter(POSTER_SIZE)}[p];'
            f"[b]scale='min({THUMBNAIL_WIDTH},iw)':-2[t]",
            '-map', '[p]', '-frames:v', '1', '-q:v', '3', poster,
            '-map', '[t]', '-frames:v', '1', '-q:v', '4', thumbnail,
        ]
    )  # fmt: skip

    interval = max(1.0, float(conf.get('SPRITE_INTERVAL')))
    columns = max(1, int(conf.get('SPRITE_COLUMNS')))
    width = max(16, int(conf.get('SPRITE_WIDTH')))
    _ffmpeg(
        [
            '-skip_frame', 'nokey', *inp, '-i', source, '-map', '0:v:0', '-an', '-sn', '-dn',
            '-vf', f"fps=1/{interval:g},scale='min({width},iw)':-2,tile={columns}x{columns}",
            '-fps_mode', 'vfr', '-q:v', '5', '-start_number', '0',
            os.path.join(folder, 'sprite-%d.jpg'),
        ]
    )  # fmt: skip
    sheets = sorted(
        glob.glob(os.path.join(folder, 'sprite-*.jpg')),
        key=lambda p: int(os.path.basename(p)[len('sprite-') : -len('.jpg')]),
    )
    if not sheets or not os.path.isfile(poster) or not os.path.isfile(thumbnail):
        raise PreviewError('ffmpeg wrote no previews')
    sheet_width, sheet_height = jpeg_size(sheets[0])
    count = min(max(1, math.ceil(duration / interval)), len(sheets) * columns * columns)
    return {
        'poster': poster,
        'thumbnail': thumbnail,
        'sprites': sheets,
        'interval': interval,
        'columns': columns,
        'tile_width': sheet_width // columns,
        'tile_height': sheet_height // columns,
        'count': count,
        'duration': duration,
    }


def upload(made: dict, video_key: str, storage) -> dict:
    """Put ``make``'s files next to the sealed ``video_key``, under its name.
    Returns the stored keys; removes what it stored if any upload fails."""
    stem = video_key[: -len('.ts')] if video_key.endswith('.ts') else video_key
    stored: list[str] = []
    try:

        def put(path, suffix):
            key = storage.upload(path, f'{stem}.{suffix}', JPEG) or f'{stem}.{suffix}'
            stored.append(key)
            return key

        keys = {
            'poster_key': put(made['poster'], 'poster.jpg'),
            'thumbnail_key': put(made['thumbnail'], 'thumb.jpg'),
            'sprites': [put(path, f'sprite-{i}.jpg') for i, path in enumerate(made['sprites'])],
        }
    except Exception:
        for key in stored:
            storage.delete(key)
        raise
    return keys


def stored_keys(keys: dict | None) -> list[str]:
    if not keys:
        return []
    return [k for k in (keys.get('poster_key'), keys.get('thumbnail_key'), *keys.get('sprites', ())) if k]


def prepare(source: str, work: str, duration: float, video_key: str, storage) -> dict | None:
    """``make`` + ``upload``, for the pipeline: None (logged) on any failure."""
    try:
        made = make(source, os.path.join(work, 'previews'), duration)
        return {**made, **upload(made, video_key, storage)}
    except Exception:
        logger.exception('vidlock: previews for %s could not be made', video_key)
        return None


def save(model, pk, video_key: str, made: dict, storage) -> None:
    """Record the stored previews of a freshly sealed video, removing the
    previous video's from storage."""
    from vidlock.models import Preview

    old = Preview.objects.filter(video_type=model._meta.label, video_id=str(pk)).first()
    Preview.objects.update_or_create(
        video_type=model._meta.label,
        video_id=str(pk),
        defaults={
            'video_key': video_key,
            'poster_key': made['poster_key'],
            'thumbnail_key': made['thumbnail_key'],
            'sprites': made['sprites'],
            'interval': made['interval'],
            'tile_width': made['tile_width'],
            'tile_height': made['tile_height'],
            'columns': made['columns'],
            'count': made['count'],
            'duration': made['duration'],
        },
    )
    if old is not None:
        fresh = set(stored_keys(made))
        for key in old.keys:
            if key not in fresh:
                storage.delete(key)


def current(video):
    """The video's previews, or None when it has none for its current file."""
    from vidlock.models import Preview, video_label

    if not video.is_sealed:
        return None
    if not conf.get('THUMBNAILS'):
        return None
    found = Preview.objects.filter(video_type=video_label(video), video_id=str(video.pk)).first()
    return found if found is not None and found.video_key == video.video_key else None


def forget(video, storage=None) -> None:
    """Remove a video's previews from the database, and from storage once the
    transaction commits (a rolled-back delete keeps its images). Called for
    you when a model using SealedVideoMixin is deleted; call it yourself when
    you drop a video's file without deleting its row."""
    from django.db import transaction

    from vidlock.models import Preview, video_label

    label = video_label(video)
    rows = Preview.objects.filter(video_type=label, video_id=str(video.pk))
    keys = [key for found in rows for key in found.keys]
    if not keys and not rows.exists():
        return
    rows.delete()

    def remove():
        try:
            target = storage
            if target is None:
                from vidlock.storage import default_storage

                target = default_storage()
            for key in keys:
                target.delete(key)
        except Exception:
            logger.exception('vidlock: could not remove the previews of %s %s', label, video.pk)

    transaction.on_commit(remove)


def on_delete(sender, instance, **kwargs) -> None:
    """``post_delete`` receiver: a deleted video takes its previews with it."""
    from vidlock.models import SealedVideoMixin

    if isinstance(instance, SealedVideoMixin):
        forget(instance)


def _stamp(seconds: float) -> str:
    ms = round(seconds * 1000)
    hours, ms = divmod(ms, 3600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1000)
    return f'{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}'


def webvtt(preview, sprite_url) -> str:
    """The seek-preview track: one cue per tile, pointing into its sheet.
    ``sprite_url(key)`` turns a sheet's object key into a URL."""
    per_sheet = preview.columns * preview.columns
    urls: dict[int, str] = {}
    lines = ['WEBVTT', '']
    for index in range(preview.count):
        sheet = index // per_sheet
        if sheet >= len(preview.sprites):
            break
        if sheet not in urls:
            urls[sheet] = sprite_url(preview.sprites[sheet])
        slot = index % per_sheet
        x = (slot % preview.columns) * preview.tile_width
        y = (slot // preview.columns) * preview.tile_height
        start = index * preview.interval
        end = min((index + 1) * preview.interval, preview.duration or (index + 1) * preview.interval)
        lines += [
            f'{_stamp(start)} --> {_stamp(end)}',
            f'{urls[sheet]}#xywh={x},{y},{preview.tile_width},{preview.tile_height}',
            '',
        ]
    return '\n'.join(lines)


def public_url(request, video, kind: str = 'poster') -> str | None:
    """Absolute URL of a video's poster (or ``kind='thumbnail'``) for pages
    shown before purchase. None unless ``POSTER_PUBLIC`` is on and the video
    has previews."""
    from django.urls import reverse

    if kind not in ('poster', 'thumbnail'):
        raise ValueError(f'kind must be poster or thumbnail, not {kind!r}')
    if not conf.get('POSTER_PUBLIC') or current(video) is None:
        return None
    return request.build_absolute_uri(reverse(f'vidlock:{kind}', args=[video.pk]))
