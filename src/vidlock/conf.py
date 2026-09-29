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
    # A named group of the settings below: 'relaxed', 'balanced' or 'strict'
    # (see PROFILES). Anything you set yourself overrides it.
    'PROFILE': 'balanced',
    # False: monitor mode. The protective rules (key pace and limits, one
    # screen, Fetch Metadata, raw keys, automatic pauses) log and signal what
    # they would refuse, and refuse nothing. Tokens, sessions and can_watch()
    # are enforced either way. Try vidlock on real viewers this way first.
    'ENFORCE': True,
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
    # Transcribe every sealed video (captions, search): a dotted path such as
    # 'vidlock.transcribe.FasterWhisper' (pip install django-vidlock[transcribe]).
    'TRANSCRIBER': None,
    # Whisper model (tiny, base, small, medium, large-v3) and the spoken
    # language ('ar', 'en', …; None detects it).
    'TRANSCRIBE_MODEL': 'small',
    'TRANSCRIBE_LANGUAGE': None,
    # How insights and reminders are sent (vidlock.notify): a dotted path to
    # fn(users, subject, message) — 'vidlock.notify.email', '.webhook' or
    # '.whatsapp'. None: manage.py vidlock_insights only prints.
    'NOTIFIER': None,
    'NOTIFY_WEBHOOK_URL': '',
    # WhatsApp Cloud API (Meta): access token, sender phone number id, and an
    # approved template (one body variable) for messages outside a 24-hour
    # conversation.
    'WHATSAPP_TOKEN': '',
    'WHATSAPP_PHONE_NUMBER_ID': '',
    'WHATSAPP_TEMPLATE': '',
    'WHATSAPP_TEMPLATE_LANGUAGE': 'ar',
    'WHATSAPP_API_VERSION': 'v21.0',
    # Direct uploads (vidlock.uploads): the largest file a browser may put in
    # the bucket, the folder new uploads go to, and how long the signed PUT
    # URL works. The bucket's CORS must allow PUT from your site.
    'MAX_UPLOAD_BYTES': 8 * 1024**3,
    'UPLOAD_PREFIX': 'uploads/',
    'UPLOAD_TTL': 3600,
    # Record who watched what (vidlock.progress, the WatchProgress model) from
    # the player's heartbeat.
    'TRACK_PROGRESS': True,
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


#: Named groups of settings: ``VIDLOCK['PROFILE']``. A setting you write
#: yourself always wins over the profile's.
PROFILES: dict[str, dict[str, Any]] = {
    # Few false alarms: for a site where sharing is not the worry yet.
    'relaxed': {
        'KEY_PACE': 4.0,
        'KEY_BURST': 12,
        'KEY_FETCHES_PER_HOUR': 60,
        'KEY_VIDEOS_PER_HOUR': 60,
        'MAX_NETWORKS_PER_DAY': None,
        'RISK_THRESHOLD': 20,
    },
    # The defaults.
    'balanced': {},
    # Paid courses that leak: one screen, keys only through the page's key
    # exchange, tighter pace, and a flagged account paused for an hour.
    'strict': {
        'KEY_ROTATION_SECONDS': 30,
        'KEY_PACE': 1.5,
        'KEY_BURST': 4,
        'KEY_VIDEOS_PER_HOUR': 15,
        'REQUIRE_WRAPPED_KEY': True,
        'STRICT_FETCH_METADATA': True,
        'MAX_STREAMS': 1,
        'MAX_NETWORKS_PER_DAY': 4,
        'RISK_SUSPEND_SECONDS': 3600,
    },
}


def get(name: str) -> Any:
    configured = getattr(settings, 'VIDLOCK', {})
    if name in configured:
        return configured[name]
    profile = PROFILES.get(configured.get('PROFILE') or 'balanced', {})
    return profile.get(name, DEFAULTS[name])


def enforcing() -> bool:
    """False in monitor mode (``ENFORCE = False``): protective rules report
    what they would refuse and let the request through."""
    return bool(get('ENFORCE'))


def load(name: str) -> Any:
    """Import the dotted path stored under ``name``, or None when unset."""
    path = get(name)
    if not path:
        return None
    try:
        return import_string(path)
    except ImportError as exc:
        raise ImproperlyConfigured(f'VIDLOCK[{name!r}] = {path!r} cannot be imported: {exc}') from exc
