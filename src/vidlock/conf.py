"""Settings, read from ``settings.VIDLOCK`` with defaults.

Read on every access rather than cached at import, so ``override_settings``
works in tests and a changed value takes effect without a restart.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

DEFAULTS: dict[str, Any] = {
    # Dotted path to your SealedBackend subclass: who may watch, how a video
    # is found. Required for the playlist and key views.
    'BACKEND': None,
    # Dotted path to a storage class, or None for S3Storage built from the
    # S3_* keys below (works with AWS S3, Cloudflare R2, MinIO, Backblaze B2).
    # 'vidlock.storage.DjangoStorage' reuses any Django storage backend
    # (django-storages for GCS, Azure, S3…) named by DJANGO_STORAGE.
    'STORAGE': None,
    'S3_BUCKET': '',
    'S3_ENDPOINT_URL': None,
    'S3_ACCESS_KEY_ID': None,
    'S3_SECRET_ACCESS_KEY': None,
    'S3_REGION': 'auto',
    # Alias in settings.STORAGES used by vidlock.storage.DjangoStorage.
    'DJANGO_STORAGE': 'default',
    'FFMPEG_BINARY': 'ffmpeg',
    # None: the ffprobe next to FFMPEG_BINARY, else 'ffprobe' on the PATH.
    'FFPROBE_BINARY': None,
    # One BYTERANGE per this many seconds. Every range is one GET against the
    # bucket; bigger segments mean fewer billed requests and coarser seeking.
    'SEGMENT_SECONDS': 10,
    # Every this many seconds of video gets its own key, and keys are handed
    # out no faster than a viewer could watch (KEY_PACE). A copied key then
    # opens a minute of a lesson, not the lesson. None: one key per video.
    'KEY_ROTATION_SECONDS': 60,
    # Keep the uploaded MP4 after sealing. Off by default: the point is that
    # no playable copy sits in the bucket. Turn it on if you want a way back
    # that does not depend on your database backups.
    'KEEP_SOURCE': False,
    # How long a playlist/key token and a signed media URL stay valid.
    'TOKEN_TTL': 600,
    # A player fetches the key about once per page load. Past this many in an
    # hour for one viewer and one video, it is a download tool.
    'KEY_FETCHES_PER_HOUR': 20,
    # A download tool harvesting a course fetches each lesson's key once, so
    # the per-video limit never sees it. Past this many *different* videos
    # keyed in an hour by one viewer, it is a harvest. None disables it.
    'KEY_VIDEOS_PER_HOUR': 30,
    # Pace of a rotated video's keys: a bucket of KEY_BURST keys per viewer and
    # video, refilled at KEY_PACE times playback speed. A viewer who seeks a
    # lot spends the burst; a tool that wants every key waits. 0 turns it off.
    'KEY_PACE': 2.0,
    'KEY_BURST': 6,
    # Refuse a web token's key to a request that carries no Fetch Metadata
    # (Sec-Fetch-*) headers. Every current browser sends them, download tools
    # do not unless told to. Off by default so an old browser keeps playing;
    # a request that says it is cross-site is refused either way.
    'STRICT_FETCH_METADATA': False,
    # Deliver web keys only through the bundled player's key exchange (ECDH:
    # the key crosses the network wrapped for that page alone, so copying it
    # from DevTools into a downloader gets nothing). Every browser with
    # MediaSource (all desktops, Android, iOS 17.1+) uses it anyway; turning
    # this on refuses the rest: generic download tools fed a copied cookie,
    # and iPhones older than iOS 17.1. (A tool written against vidlock's own
    # protocol can still do the exchange; the pace and risk score remain.)
    # Off: raw fetches only count towards the risk score.
    'REQUIRE_WRAPPED_KEY': False,
    # Streams one viewer may play at once, on different devices (browser
    # sessions or app logins). A new device past the limit takes over the
    # oldest one, which stops with a message. None: no limit.
    'MAX_STREAMS': None,
    # A stream whose player has not sent a heartbeat for this long lapses.
    'STREAM_TIMEOUT': 90,
    # How often the bundled player sends its heartbeat.
    'HEARTBEAT_SECONDS': 30,
    # Suspicion score (see vidlock.risk): event weights, the daily score that
    # flags a viewer, and how long a flagged viewer is refused keys (0: flag
    # only; listen to vidlock.signals.viewer_flagged).
    'RISK_WEIGHTS': {},
    'RISK_THRESHOLD': 10,
    'RISK_SUSPEND_SECONDS': 0,
    # Networks (/24, /48) one viewer may come from in a day before each new
    # one counts towards the risk score. None: not counted.
    'MAX_NETWORKS_PER_DAY': 6,
    # Watermark: add a short code that `manage.py vidlock_trace` turns back
    # into the viewer, plus the date and time, to the text drawn over the video.
    'WATERMARK_CODE': True,
    # Optional dotted path: called as fn(request, user, video) the first time a
    # viewer crosses a key limit in a day. Alert, log, ban — yours. The
    # vidlock.signals.key_abuse signal fires as well, with the reason.
    'ON_KEY_ABUSE': None,
    # Secrets that encrypt the per-video keys stored in your database. The
    # first one encrypts; all are tried when decrypting, so you can rotate by
    # putting a new one first and running `manage.py vidlock_rewrap`. Empty:
    # derived from SECRET_KEY (and SECRET_KEY_FALLBACKS).
    'KEY_ENCRYPTION_KEYS': [],
    # Optional dotted path: fn(model, pk, video_key) that queues sealing in
    # your task runner. Used by the admin's "Seal again" action; unset, the
    # action seals inline in the request.
    'ENQUEUE_SEAL': None,
    # Where the player loads hls.js from. None: the copy bundled with vidlock
    # (served from your static files). A CDN URL works too; pair it with
    # HLS_JS_INTEGRITY.
    'HLS_JS_URL': None,
    'HLS_JS_INTEGRITY': '',
}


def get(name: str) -> Any:
    return getattr(settings, 'VIDLOCK', {}).get(name, DEFAULTS[name])


def load(name: str) -> Any:
    """Import the dotted path stored under ``name``, or None when unset."""
    path = get(name)
    if not path:
        return None
    try:
        return import_string(path)
    except ImportError as exc:
        raise ImproperlyConfigured(f'VIDLOCK[{name!r}] = {path!r} cannot be imported: {exc}') from exc
