from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class VidlockConfig(AppConfig):
    name = 'vidlock'
    verbose_name = _('VidLock')

    def ready(self):
        from vidlock import checks  # noqa: F401 — registers the system checks
