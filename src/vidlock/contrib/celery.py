"""Sealing with Celery.

    VIDLOCK = {..., 'ENQUEUE_SEAL': 'vidlock.contrib.celery.enqueue'}

The task takes the model's label rather than the class, so it serialises as
JSON. It is ``acks_late``: a worker that dies mid-ffmpeg leaves the message
for another, and ``seal`` is idempotent.
"""

from celery import shared_task
from django.apps import apps

from vidlock.pipeline import seal


@shared_task(name='vidlock.seal', acks_late=True, ignore_result=True)
def seal_task(label: str, pk, video_key: str) -> str:
    return seal(apps.get_model(label), pk, video_key)


def enqueue(model, pk, video_key: str) -> None:
    seal_task.delay(model._meta.label, pk, video_key)
