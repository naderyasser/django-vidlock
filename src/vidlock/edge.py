"""Edge delivery: sealed media through a Cloudflare Worker in front of R2.

Opt-in (``VIDLOCK['DELIVERY'] = 'edge'``). By default (``'direct'``) the
player reads the sealed .ts from presigned bucket URLs, as before: every
byte range is a request to R2, with no cache in between.

In edge mode the media URL in ``playback_info`` and in each playlist points
at ``EDGE_URL`` instead, signed here with HMAC-SHA256 under
``EDGE_SECRETS[0]``:

    https://media.example/<object key>?exp=<unix time>&v=<viewer>&sig=<hex>

The Worker (``edge/cloudflare-worker``) checks the signature against each of
its secrets and the expiry, reads the object through an R2 binding (the
bucket stays private), answers Range requests, and caches each range under a
key without the token, so every viewer shares the cache. That is safe for
one reason only: the bytes are AES-128 encrypted, and the keys never pass
through the Worker. Keys and playlists stay on Django, behind every check
they have today.

What is signed:

* ``v1``, a version, so the format can change without old URLs passing;
* the object key (the Worker serves ``.ts`` objects and nothing else);
* the expiry, as for a presigned URL (``TOKEN_TTL``, or the length of the
  video plus ``TOKEN_TTL`` in a playlist, as today);
* ``v``, a short tag of the viewer and their stream lease. The Worker
  cannot check a session, so a shared URL still plays until it expires,
  exactly as a presigned URL does; the tag makes it name who it was issued
  to in the Worker's logs.

Rotating: put the new secret first in ``EDGE_SECRETS`` *and* add it to the
Worker's ``VIDLOCK_EDGE_SECRETS``; once the longest-lived URLs signed with
the old one have expired, remove it from both.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from urllib.parse import quote, urlencode

from django.core.exceptions import ImproperlyConfigured
from django.utils.crypto import salted_hmac

from vidlock import conf

VERSION = 'v1'
#: The Worker refuses URLs that claim to live longer than this.
MAX_TTL = 7 * 24 * 3600
#: Object keys the Worker will serve: the sealed files vidlock writes.
SEALED_SUFFIX = '.ts'
#: What the Worker accepts as ``v``.
_VIEWER = re.compile(r'[0-9a-f]{0,32}')


def enabled() -> bool:
    return conf.get('DELIVERY') == 'edge'


def secrets() -> list[bytes]:
    found = [s.encode() if isinstance(s, str) else bytes(s) for s in conf.get('EDGE_SECRETS') or [] if s]
    if not found:
        raise ImproperlyConfigured("VIDLOCK['DELIVERY'] = 'edge' needs VIDLOCK['EDGE_SECRETS'].")
    return found


def message(key: str, expires: int, viewer: str) -> bytes:
    return f'{VERSION}\n{key}\n{int(expires)}\n{viewer}'.encode()


def signature(secret: bytes, key: str, expires: int, viewer: str) -> str:
    return hmac.new(secret, message(key, expires, viewer), hashlib.sha256).hexdigest()


def viewer_tag(user_pk, lease: str = '') -> str:
    """12 hex characters naming one viewer's stream, readable only with
    SECRET_KEY: ``trace``-able in the Worker's logs, not a user id."""
    return salted_hmac('vidlock.edge.viewer', f'{user_pk}|{lease}', algorithm='sha256').hexdigest()[:12]


def url_for(key: str, ttl: int, viewer: str = '', now: float | None = None) -> str:
    """The signed edge URL of the sealed object ``key``, valid ``ttl`` seconds."""
    if not key.endswith(SEALED_SUFFIX):
        raise ValueError(f'edge delivery serves sealed {SEALED_SUFFIX} objects only, not {key!r}')
    if not _VIEWER.fullmatch(viewer):
        raise ValueError(f'a viewer tag is up to 32 lowercase hex characters (viewer_tag()), not {viewer!r}')
    base = (conf.get('EDGE_URL') or '').rstrip('/')
    if not base:
        raise ImproperlyConfigured("VIDLOCK['DELIVERY'] = 'edge' needs VIDLOCK['EDGE_URL'].")
    expires = int(now if now is not None else time.time()) + max(1, min(int(ttl), MAX_TTL))
    sig = signature(secrets()[0], key, expires, viewer)
    query = urlencode({'exp': expires, 'v': viewer, 'sig': sig})
    return f'{base}/{quote(key, safe="/")}?{query}'


def verify(key: str, expires: int, viewer: str, sig: str, now: float | None = None) -> bool:
    """What the Worker checks, in Python: for tests and ``vidlock_doctor``."""
    now = int(now if now is not None else time.time())
    if not key.endswith(SEALED_SUFFIX) or expires < now or expires > now + MAX_TTL:
        return False
    return any(hmac.compare_digest(signature(s, key, expires, viewer), sig) for s in secrets())


def media_url(storage, video, ttl: int, user_pk=None, lease: str = '') -> str:
    """Where the player reads a *sealed* video's bytes: the edge in edge mode,
    a presigned bucket URL otherwise (and always for the unsealed MP4)."""
    if enabled() and video.is_sealed and video.video_key.endswith(SEALED_SUFFIX):
        return url_for(video.video_key, ttl, viewer_tag(user_pk, lease) if user_pk is not None else '')
    return storage.signed_url(video.video_key, ttl)
