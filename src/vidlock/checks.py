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
        ('ENQUEUE_TRANSCODE', 'vidlock.E006'),
    ):
        try:
            conf.load(name)
        except ImproperlyConfigured as exc:
            problems.append(Error(str(exc), id=error_id))

    if user.get('PROFILE', 'balanced') not in conf.PROFILES:
        problems.append(
            Error(
                f"VIDLOCK['PROFILE'] = {user.get('PROFILE')!r} is not one of {', '.join(conf.PROFILES)}.",
                id='vidlock.E005',
            )
        )
    if not conf.enforcing():
        problems.append(
            Warning(
                'vidlock is in monitor mode: protective rules count what they would refuse, '
                'and refuse nothing.',
                hint="See manage.py vidlock_status, then set VIDLOCK['ENFORCE'] = True.",
                id='vidlock.W008',
            )
        )

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

    backend = str(getattr(settings, 'CACHES', {}).get('default', {}).get('BACKEND', ''))
    if backend.endswith(('LocMemCache', 'DummyCache')):
        problems.append(
            Warning(
                f'The default cache ({backend.rsplit(".", 1)[-1]}) is not shared between processes.',
                hint=(
                    'Key limits, stream leases and risk scores live in the cache; with several '
                    'workers each keeps its own. Use Redis, Memcached or the database cache.'
                ),
                id='vidlock.W007',
            )
        )

    problems.extend(_transcode_problems())
    problems.extend(_edge_problems())

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


def _transcode_problems():
    from vidlock import transcode

    problems = []
    choice = conf.get('TRANSCODE_ENCODER') or 'auto'
    if choice != 'auto' and choice not in transcode.ENCODERS:
        problems.append(
            Error(
                f"VIDLOCK['TRANSCODE_ENCODER'] = {choice!r} is not one of "
                f"auto, {', '.join(transcode.ENCODERS)}.",
                id='vidlock.E007',
            )
        )
    if not conf.get('TRANSCODE'):
        return problems
    if not conf.get('ENQUEUE_TRANSCODE'):
        timeout = int(conf.get('TRANSCODE_TIMEOUT'))
        problems.append(
            Warning(
                "VIDLOCK['TRANSCODE'] is on but ENQUEUE_TRANSCODE is not set: every re-encode runs "
                'inside the seal job that found it. One long HEVC or 4K upload can then block the '
                f'seal queue for hours, and a worker time limit shorter than TRANSCODE_TIMEOUT '
                f'({timeout} s) kills the encode halfway and leaves the video unsealed.',
                hint=(
                    "Set ENQUEUE_TRANSCODE (e.g. 'vidlock.contrib.celery.enqueue_transcode') and run "
                    f"a dedicated worker for the {conf.get('TRANSCODE_QUEUE')!r} queue with "
                    'concurrency 1 and a time limit of at least TRANSCODE_TIMEOUT.'
                ),
                id='vidlock.W009',
            )
        )
    binary = packager.ffmpeg_binary()
    if binary:
        missing = {'libx264', 'aac'} - transcode.compiled_encoders(binary)
        if missing:
            problems.append(
                Warning(
                    f'ffmpeg ({binary}) has no {" or ".join(sorted(missing))} encoder; '
                    're-encodes will fail on this machine.',
                    hint='Install a full ffmpeg build (Debian/Ubuntu: apt install ffmpeg) on the worker.',
                    id='vidlock.W010',
                )
            )
    return problems


def _edge_problems():
    problems = []
    delivery = conf.get('DELIVERY')
    if delivery not in ('direct', 'edge'):
        return [Error(f"VIDLOCK['DELIVERY'] = {delivery!r} is not 'direct' or 'edge'.", id='vidlock.E008')]
    if delivery != 'edge':
        return problems
    url = conf.get('EDGE_URL') or ''
    found = [s for s in conf.get('EDGE_SECRETS') or [] if s]
    if not url or not found:
        problems.append(
            Error(
                "VIDLOCK['DELIVERY'] = 'edge' needs EDGE_URL (the Worker's domain) and EDGE_SECRETS.",
                hint='See edge/cloudflare-worker/README.md.',
                id='vidlock.E009',
            )
        )
    if url and not url.startswith('https://'):
        problems.append(
            Warning(
                f"VIDLOCK['EDGE_URL'] = {url!r} is not HTTPS: signed media URLs would travel in the clear.",
                id='vidlock.W011',
            )
        )
    if any(len(s) < 32 for s in found):
        problems.append(
            Warning(
                'An EDGE_SECRETS entry is shorter than 32 characters.',
                hint='python -c "import secrets; print(secrets.token_urlsafe(48))"',
                id='vidlock.W012',
            )
        )
    return problems
