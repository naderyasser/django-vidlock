from django.db import models

from vidlock.models import SealedVideoMixin


class Lesson(SealedVideoMixin):
    title = models.CharField(max_length=200)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('-created',)

    def __str__(self):
        return self.title
