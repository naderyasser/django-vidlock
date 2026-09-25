"""Seal one uploaded video: download, remux + encrypt, upload, swap, delete.

``seal`` is synchronous and idempotent. Run it wherever your background work
runs — a Celery task, an RQ job, a thread, a management command:

    from vidlock.pipeline import seal
    seal(Lesson, lesson.pk, lesson.video_key)

The video may be replaced or deleted while ffmpeg runs, so the swap re-reads
the row under a lock and throws its own work away if the key moved. Any
failure leaves the uploaded MP4 exactly as it was: sealing only ever
improves a video, it never takes one down.
"""

import logging
import os
import tempfile
import uuid

from django.db import transaction

from vidlock import packager
from vidlock.playlist import segment_count
from vidlock.storage import default_storage

logger = logging.getLogger(__name__)

TS_CONTENT_TYPE = 'video/mp2t'


def wants(key):
    """Worth queueing: ffmpeg is here and the upload is a container that
    usually holds H.264. ``seal`` still probes and may skip it."""
    return packager.available() and os.path.splitext(key or '')[1].lower() in packager.SOURCE_EXTENSIONS


def sealed_key_for(source_key):
    """The .ts sits next to the source, under a new random name."""
    folder = source_key.rsplit('/', 1)[0] + '/' if '/' in source_key else ''
    return f'{folder}{uuid.uuid4().hex}.ts'


def _mark(model, pk, source_key, state, error):
    model._default_manager.filter(pk=pk, video_key=source_key).update(
        sealed_state=state, sealed_error=error[:300]
    )
    return state


def seal(model, pk, source_key, storage=None, delete_source=True):
    """Returns the resulting state: 'sealed', 'skipped', 'failed' or 'stale'."""
    storage = storage or default_storage()
    row = model._default_manager.filter(pk=pk).values_list('video_key', flat=True).first()
    if row != source_key:
        return 'stale'
    if not packager.available():
        return _mark(model, pk, source_key, model.STATE_SKIPPED, 'ffmpeg is not installed')

    target = sealed_key_for(source_key)
    try:
        with tempfile.TemporaryDirectory(prefix='vidlock-') as work:
            source = os.path.join(work, 'source' + os.path.splitext(source_key)[1].lower())
            storage.download(source_key, source)
            video, audio = packager.probe(source)
            if not packager.can_seal(video, audio):
                logger.info('%s %s not sealed: %s/%s', model.__name__, pk, video, audio)
                return _mark(
                    model,
                    pk,
                    source_key,
                    model.STATE_SKIPPED,
                    f'{video or "?"}/{audio or "-"} needs a re-encode to H.264/AAC',
                )
            ts_path, playlist, key = packager.package(source, work)
            size = os.path.getsize(ts_path)
            storage.upload(ts_path, target, TS_CONTENT_TYPE)
    except Exception as exc:  # noqa: BLE001 — the MP4 keeps playing whatever went wrong
        logger.exception('sealing %s %s failed', model.__name__, pk)
        storage.delete(target)
        return _mark(model, pk, source_key, model.STATE_FAILED, f'{type(exc).__name__}: {exc}')

    try:
        with transaction.atomic():
            current = (
                model._default_manager.select_for_update().filter(pk=pk).values_list('video_key', flat=True)
            )
            if list(current) != [source_key]:
                storage.delete(target)
                return 'stale'
            model._default_manager.filter(pk=pk).update(
                video_key=target,
                video_size=size,
                sealed_state=model.STATE_SEALED,
                sealed_playlist=playlist,
                sealed_key=key,
                sealed_error='',
            )
    except Exception:
        # An object no row names is billed for ever and shows on no meter.
        storage.delete(target)
        raise
    # Only after the row points at the .ts: a viewer halfway through the MP4
    # gets an error, and a player that retries receives the sealed stream.
    if delete_source:
        storage.delete(source_key)
    logger.info('%s %s sealed (%s bytes, %s segments)', model.__name__, pk, size, segment_count(playlist))
    return model.STATE_SEALED
