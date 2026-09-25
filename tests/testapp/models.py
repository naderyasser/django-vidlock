from django.conf import settings
from django.db import models

from vidlock.models import SealedVideoMixin


class Lesson(SealedVideoMixin):
    title = models.CharField(max_length=100)
    students = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True)
