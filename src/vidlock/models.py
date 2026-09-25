"""The fields a sealed video needs, as an abstract mixin for your own model.

The key and the playlist live in your database, not next to the video: the
file in the bucket is worthless on its own, and the only way to the key is a
view that asks your backend whether this viewer may watch.
"""

from django.db import models


class SealedVideoMixin(models.Model):
    STATE_NONE, STATE_PENDING, STATE_SEALED, STATE_SKIPPED, STATE_FAILED = (
        '',
        'pending',
        'sealed',
        'skipped',
        'failed',
    )

    #: Object key in storage: the uploaded MP4 until sealing, the .ts after.
    video_key = models.CharField(max_length=500, blank=True, default='', db_index=True)
    video_size = models.BigIntegerField(default=0)
    sealed_state = models.CharField(max_length=10, blank=True, default='')
    sealed_playlist = models.TextField(blank=True, default='')
    sealed_key = models.BinaryField(null=True, blank=True, editable=False)
    sealed_error = models.CharField(max_length=300, blank=True, default='')

    class Meta:
        abstract = True

    @property
    def is_sealed(self):
        """True once ``video_key`` names the encrypted .ts rather than the MP4."""
        return bool(self.sealed_state == self.STATE_SEALED and self.sealed_playlist and self.sealed_key)

    def forget_seal(self):
        """Call when the video is replaced or removed — the old seal belongs to
        the old file, and a stale one would make a new MP4 look sealed."""
        self.sealed_state = self.STATE_NONE
        self.sealed_playlist = ''
        self.sealed_key = None
        self.sealed_error = ''
