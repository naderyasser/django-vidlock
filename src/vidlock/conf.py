"""Settings, read from ``settings.VIDLOCK`` with defaults.

Read on every access rather than cached at import, so ``override_settings``
works in tests and a changed value takes effect without a restart.
"""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

DEFAULTS = {
    # Dotted path to your SealedBackend subclass: who may watch, how a video
    # is found. Required for the playlist and key views.
    'BACKEND': None,
    # Dotted path to a storage class, or None for S3Storage built from the
    # S3_* keys below (works with AWS S3, Cloudflare R2, MinIO, Backblaze B2).
    'STORAGE': None,
    'S3_BUCKET': '',
    'S3_ENDPOINT_URL': None,
    'S3_ACCESS_KEY_ID': None,
    'S3_SECRET_ACCESS_KEY': None,
    'S3_REGION': 'auto',
    'FFMPEG_BINARY': 'ffmpeg',
    # One BYTERANGE per this many seconds. Every range is one GET against the
    # bucket; bigger segments mean fewer billed requests and coarser seeking.
    'SEGMENT_SECONDS': 10,
    # How long a playlist/key token and a signed media URL stay valid.
    'TOKEN_TTL': 600,
    # A player fetches the key about once per page load. Past this many in an
    # hour for one viewer and one video, it is a download tool.
    'KEY_FETCHES_PER_HOUR': 20,
    # Optional dotted path: called as fn(request, user, video) the first time a
    # viewer crosses KEY_FETCHES_PER_HOUR in a day. Alert, log, ban — yours.
    'ON_KEY_ABUSE': None,
    # Where the player loads hls.js from when a video is sealed.
    'HLS_JS_URL': 'https://cdn.jsdelivr.net/npm/hls.js@1.5.20/dist/hls.light.min.js',
}


def get(name):
    return getattr(settings, 'VIDLOCK', {}).get(name, DEFAULTS[name])


def load(name):
    """Import the dotted path stored under ``name``, or None when unset."""
    path = get(name)
    if not path:
        return None
    try:
        return import_string(path)
    except ImportError as exc:
        raise ImproperlyConfigured(f'VIDLOCK[{name!r}] = {path!r} cannot be imported: {exc}') from exc
