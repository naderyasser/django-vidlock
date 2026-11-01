"""Sealing and re-encoding with Celery.

    VIDLOCK = {
        ...,
        'ENQUEUE_SEAL': 'vidlock.contrib.celery.enqueue',
        'ENQUEUE_TRANSCODE': 'vidlock.contrib.celery.enqueue_transcode',
    }

The tasks take the model's label rather than the class, so they serialise as
JSON. They are ``acks_late``: a worker that dies mid-ffmpeg leaves the
message for another, and both jobs are idempotent.

Re-encodes go to the queue named by ``VIDLOCK['TRANSCODE_QUEUE']``
(``'vidlock-transcode'``), with a time limit just above
``TRANSCODE_TIMEOUT``. Give that queue a worker of its own:

    celery -A proj worker -Q vidlock-transcode --concurrency 1 --prefetch-multiplier 1

With Redis or SQS as the broker, raise the visibility timeout above
TRANSCODE_TIMEOUT too, or a long encode is handed to a second worker while
the first is still on it:

    CELERY_BROKER_TRANSPORT_OPTIONS = {'visibility_timeout': 5 * 3600}
"""

from celery import shared_task
from django.apps import apps

from vidlock import conf, transcode
from vidlock.pipeline import seal


@shared_task(name='vidlock.seal', acks_late=True, ignore_result=True)
def seal_task(label: str, pk, video_key: str) -> str:
    return seal(apps.get_model(label), pk, video_key)


@shared_task(name='vidlock.transcode', acks_late=True, ignore_result=True)
def transcode_task(label: str, pk, video_key: str) -> str:
    return transcode.run(apps.get_model(label), pk, video_key)


def enqueue(model, pk, video_key: str) -> None:
    seal_task.delay(model._meta.label, pk, video_key)


def enqueue_transcode(model, pk, video_key: str) -> None:
    limit = int(conf.get('TRANSCODE_TIMEOUT'))
    transcode_task.apply_async(
        (model._meta.label, pk, video_key),
        queue=conf.get('TRANSCODE_QUEUE'),
        # ffmpeg is killed at TRANSCODE_TIMEOUT; leave room to record why.
        soft_time_limit=limit + 300,
        time_limit=limit + 600,
    )
