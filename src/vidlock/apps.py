from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class VidlockConfig(AppConfig):
    name = 'vidlock'
    verbose_name = _('VidLock')

    def ready(self):
        from django.db.models.signals import post_delete

        from vidlock import (
            checks,  # noqa: F401 — registers the system checks
            previews,
        )

        # Deleting a video deletes its previews (vidlock.previews.forget).
        post_delete.connect(previews.on_delete, dispatch_uid='vidlock.previews.on_delete')
