"""Sealing with Django's own tasks framework (Django 6.0+).

    VIDLOCK = {..., 'ENQUEUE_SEAL': 'vidlock.contrib.django_tasks.enqueue'}

Needs a ``TASKS`` backend that runs tasks in the background (a database or
queue backend); the default ``ImmediateBackend`` seals inside the request.
"""

from django.apps import apps
from django.tasks import task

from vidlock.pipeline import seal


@task
def seal_task(label: str, pk, video_key: str) -> str:
    return seal(apps.get_model(label), pk, video_key)


def enqueue(model, pk, video_key: str) -> None:
    seal_task.enqueue(model._meta.label, pk, video_key)
