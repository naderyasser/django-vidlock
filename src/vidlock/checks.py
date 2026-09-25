"""System checks: ``manage.py check`` says what is missing before a viewer does."""

from django.conf import settings
from django.core.checks import Error, Tags, Warning, register
from django.core.exceptions import ImproperlyConfigured

from vidlock import conf, packager


@register(Tags.compatibility)
def check_settings(app_configs, **kwargs):
    problems = []
    user = getattr(settings, 'VIDLOCK', {})
    if not isinstance(user, dict):
        return [Error('settings.VIDLOCK must be a dict.', id='vidlock.E000')]

    unknown = sorted(set(user) - set(conf.DEFAULTS))
    if unknown:
        problems.append(
            Warning(
                f'Unknown VIDLOCK settings: {", ".join(unknown)}.',
                hint='A typo here silently falls back to the default.',
                id='vidlock.W001',
            )
        )

    for name, error_id in (
        ('BACKEND', 'vidlock.E001'),
        ('STORAGE', 'vidlock.E002'),
        ('ON_KEY_ABUSE', 'vidlock.E003'),
        ('ENQUEUE_SEAL', 'vidlock.E004'),
    ):
        try:
            conf.load(name)
        except ImproperlyConfigured as exc:
            problems.append(Error(str(exc), id=error_id))

    if not conf.get('BACKEND'):
        problems.append(
            Warning(
                "VIDLOCK['BACKEND'] is not set; the playlist and key views will fail.",
                hint='Point it at your vidlock.backend.SealedBackend subclass.',
                id='vidlock.W002',
            )
        )

    if not conf.get('STORAGE') and not conf.get('S3_BUCKET'):
        problems.append(
            Warning(
                "VIDLOCK['S3_BUCKET'] is empty and no VIDLOCK['STORAGE'] is set.",
                hint="Set the S3_* keys, or STORAGE = 'vidlock.storage.DjangoStorage'.",
                id='vidlock.W003',
            )
        )

    if not packager.available():
        problems.append(
            Warning(
                f"ffmpeg ({conf.get('FFMPEG_BINARY')!r}) is not on this machine; "
                'uploads will not be sealed here.',
                hint='Fine on web servers; install ffmpeg on the worker that runs vidlock.pipeline.seal.',
                id='vidlock.W004',
            )
        )

    if int(conf.get('TOKEN_TTL')) < 60:
        problems.append(
            Warning(
                'VIDLOCK["TOKEN_TTL"] under 60 seconds makes players renew constantly.', id='vidlock.W005'
            )
        )

    if not conf.get('KEY_ENCRYPTION_KEYS'):
        problems.append(
            Warning(
                'Video keys are encrypted with a secret derived from SECRET_KEY.',
                hint=(
                    "Set VIDLOCK['KEY_ENCRYPTION_KEYS'] to a secret of its own, so rotating "
                    'SECRET_KEY cannot lock every video. Run `manage.py vidlock_rewrap` after.'
                ),
                id='vidlock.W006',
            )
        )
    return problems
