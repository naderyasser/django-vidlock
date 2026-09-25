"""The fields a sealed video needs, as an abstract mixin for your own model.

The key and the playlist live in your database, not next to the video: the
file in the bucket is worthless on its own, and the only way to the key is a
view that asks your backend whether this viewer may watch. The key itself is
stored encrypted under a secret from your settings (see ``vidlock.keys``).
"""

from __future__ import annotations

from django.db import models

from vidlock import keys
from vidlock.playlist import duration, key_count


def content_key(keys_blob: bytes, index: int) -> bytes:
    """Key ``index`` out of a video's concatenated 16-byte keys."""
    if index < 0 or (index + 1) * 16 > len(keys_blob):
        raise IndexError(f'no key {index}')
    return bytes(keys_blob[index * 16 : index * 16 + 16])


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
    #: The content key, wrapped by ``vidlock.keys.wrap``. Use ``content_key()``.
    sealed_key = models.BinaryField(null=True, blank=True, editable=False)
    sealed_error = models.CharField(max_length=300, blank=True, default='')

    class Meta:
        abstract = True

    @property
    def is_sealed(self) -> bool:
        """True once ``video_key`` names the encrypted .ts rather than the MP4."""
        return bool(self.sealed_state == self.STATE_SEALED and self.sealed_playlist and self.sealed_key)

    @property
    def sealed_duration(self) -> float:
        """Seconds of video in the sealed playlist; 0.0 before sealing."""
        return duration(self.sealed_playlist) if self.sealed_playlist else 0.0

    def content_key(self, index: int = 0) -> bytes:
        """The 16-byte AES key for rotation period ``index``, decrypted. Raises
        ``vidlock.keys.KeyUnwrapError`` when no configured secret opens it and
        ``IndexError`` past the last key."""
        return content_key(keys.unwrap(self.sealed_key), index)

    @property
    def key_count(self) -> int:
        """How many keys the sealed video uses (one per rotation period)."""
        return key_count(self.sealed_playlist) if self.sealed_playlist else 0

    def forget_seal(self) -> None:
        """Call when the video is replaced or removed — the old seal belongs to
        the old file, and a stale one would make a new MP4 look sealed."""
        self.sealed_state = self.STATE_NONE
        self.sealed_playlist = ''
        self.sealed_key = None
        self.sealed_error = ''


def sealed_models(labels=None):
    """Your concrete models that use the mixin; ``labels`` ('app.Model')
    narrows them down. Used by the management commands."""
    from django.apps import apps
    from django.core.exceptions import ImproperlyConfigured

    if labels:
        found = []
        for label in labels:
            try:
                model = apps.get_model(label)
            except (LookupError, ValueError) as exc:
                raise ImproperlyConfigured(f'{label!r} is not a model: {exc}') from exc
            if not issubclass(model, SealedVideoMixin):
                raise ImproperlyConfigured(f'{label} does not use vidlock.models.SealedVideoMixin')
            found.append(model)
        return found
    return [m for m in apps.get_models() if issubclass(m, SealedVideoMixin) and not m._meta.abstract]
