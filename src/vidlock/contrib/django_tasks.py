"""Sealing and re-encoding with Django's own tasks framework (Django 6.0+).

    VIDLOCK = {
        ...,
        'ENQUEUE_SEAL': 'vidlock.contrib.django_tasks.enqueue',
        'ENQUEUE_TRANSCODE': 'vidlock.contrib.django_tasks.enqueue_transcode',
    }

Needs a ``TASKS`` backend that runs tasks in the background (a database or
queue backend); the default ``ImmediateBackend`` seals inside the request.
Re-encodes go to the queue ``VIDLOCK['TRANSCODE_QUEUE']``
(``'vidlock-transcode'``): list it in the backend's ``QUEUES`` and run a
worker for that queue alone, one task at a time.
"""

from django.apps import apps
from django.tasks import task

from vidlock import conf, transcode
from vidlock.pipeline import seal


@task
def seal_task(label: str, pk, video_key: str) -> str:
    return seal(apps.get_model(label), pk, video_key)


@task
def transcode_task(label: str, pk, video_key: str) -> str:
    return transcode.run(apps.get_model(label), pk, video_key)


def enqueue(model, pk, video_key: str) -> None:
    seal_task.enqueue(model._meta.label, pk, video_key)


def enqueue_transcode(model, pk, video_key: str) -> None:
    transcode_task.using(queue_name=conf.get('TRANSCODE_QUEUE')).enqueue(model._meta.label, pk, video_key)
