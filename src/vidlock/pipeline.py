"""Seal one uploaded video: download, remux + encrypt, upload, swap, delete.

``seal`` is synchronous and idempotent. Run it wherever your background work
runs — a Celery task, an RQ job, a thread, a management command:

    from vidlock.pipeline import seal
    seal(Lesson, lesson.pk, lesson.video_key)

The video may be replaced or deleted while ffmpeg runs, so the swap re-reads
the row under a lock and throws its own work away if the key moved. Any
failure leaves the uploaded MP4 exactly as it was: sealing only ever
improves a video, it never takes one down.

Every outcome is announced through ``vidlock.signals.seal_finished``.
"""

from __future__ import annotations

import logging
import os
import tempfile
import uuid

from django.db import transaction

from vidlock import conf, keys, packager, signals
from vidlock.playlist import segment_count
from vidlock.storage import default_storage

logger = logging.getLogger(__name__)

TS_CONTENT_TYPE = 'video/mp2t'


def wants(key: str | None) -> bool:
    """Worth queueing: ffmpeg is here and the upload is a container that
    usually holds H.264. ``seal`` still probes and may skip it."""
    return packager.available() and os.path.splitext(key or '')[1].lower() in packager.SOURCE_EXTENSIONS


def seal_later(instance, video_key: str | None = None) -> bool:
    """Record a new upload on ``instance`` and queue its sealing after the
    transaction commits, through ``VIDLOCK['ENQUEUE_SEAL']`` (inline when
    unset). Returns whether sealing was queued. Replaces the four lines of
    bookkeeping in the README:

        lesson.video_key = uploaded_key
        seal_later(lesson)
    """
    key = video_key or instance.video_key
    model = type(instance)
    instance.forget_seal()
    instance.video_key = key
    queued = wants(key)
    instance.sealed_state = model.STATE_PENDING if queued else model.STATE_NONE
    instance.save()
    if queued:
        enqueue = conf.load('ENQUEUE_SEAL')
        pk = instance.pk
        transaction.on_commit(
            (lambda: enqueue(model, pk, key)) if enqueue else (lambda: seal(model, pk, key))
        )
    return queued


def sealed_key_for(source_key: str) -> str:
    """The .ts sits next to the source, under a new random name."""
    folder = source_key.rsplit('/', 1)[0] + '/' if '/' in source_key else ''
    return f'{folder}{uuid.uuid4().hex}.ts'


def _finished(model, pk, state, error='', video_key=''):
    signals.seal_finished.send(sender=model, pk=pk, state=state, error=error, video_key=video_key)
    return state


def _mark(model, pk, source_key, state, error):
    model._default_manager.filter(pk=pk, video_key=source_key).update(
        sealed_state=state, sealed_error=error[:300]
    )
    return _finished(model, pk, state, error[:300], source_key)


def seal(model, pk, source_key: str, storage=None, delete_source: bool | None = None) -> str:
    """Returns the resulting state: 'sealed', 'skipped', 'failed' or 'stale'.

    ``delete_source`` defaults to ``not VIDLOCK['KEEP_SOURCE']``.
    """
    storage = storage or default_storage()
    if delete_source is None:
        delete_source = not conf.get('KEEP_SOURCE')
    row = model._default_manager.filter(pk=pk).values_list('video_key', flat=True).first()
    if row != source_key:
        return _finished(model, pk, 'stale', video_key=row or '')
    if not packager.available():
        return _mark(model, pk, source_key, model.STATE_SKIPPED, 'ffmpeg is not installed')

    target = sealed_key_for(source_key)
    try:
        with tempfile.TemporaryDirectory(prefix='vidlock-') as work:
            source = os.path.join(work, 'source' + os.path.splitext(source_key)[1].lower())
            storage.download(source_key, source)
            found = packager.probe(source)
            if not isinstance(found, packager.Probe):
                found = packager.Probe(*found)
            if found.problem:
                logger.info('%s %s not sealed: %s', model.__name__, pk, found.problem)
                return _mark(model, pk, source_key, model.STATE_SKIPPED, found.problem)
            ts_path, playlist, key = packager.package(source, work)
            size = os.path.getsize(ts_path)
            # A storage may save under another name (Django storages avoid
            # overwrites); the name it returns is the one that exists.
            target = storage.upload(ts_path, target, TS_CONTENT_TYPE) or target
    except Exception as exc:
        logger.exception('sealing %s %s failed', model.__name__, pk)
        storage.delete(target)
        return _mark(model, pk, source_key, model.STATE_FAILED, f'{type(exc).__name__}: {exc}')

    try:
        with transaction.atomic():
            current = list(
                model._default_manager.select_for_update().filter(pk=pk).values_list('video_key', flat=True)
            )
            if current == [source_key]:
                model._default_manager.filter(pk=pk).update(
                    video_key=target,
                    video_size=size,
                    sealed_state=model.STATE_SEALED,
                    sealed_playlist=playlist,
                    sealed_key=keys.wrap(key),
                    sealed_error='',
                )
    except Exception:
        # An object no row names is billed for ever and shows on no meter.
        storage.delete(target)
        raise
    if current != [source_key]:
        storage.delete(target)
        return _finished(model, pk, 'stale', video_key=(current or [''])[0])
    # Only after the row points at the .ts: a viewer halfway through the MP4
    # gets an error, and a player that retries receives the sealed stream.
    if delete_source:
        storage.delete(source_key)
    logger.info('%s %s sealed (%s bytes, %s segments)', model.__name__, pk, size, segment_count(playlist))
    return _finished(model, pk, model.STATE_SEALED, video_key=target)
