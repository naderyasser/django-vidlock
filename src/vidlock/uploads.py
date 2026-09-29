"""Uploads that go from the browser straight to the bucket.

A 2 GB lesson sent through Django ties up a worker for minutes and needs the
disk to hold it. Here Django only signs: the browser asks for a place to put
the file, PUTs it to the bucket itself (with a progress bar), and hands back
a ticket that names what it uploaded:

    1. POST {name, size} to vidlock:upload     -> {ticket, url, headers}
    2. PUT the file to url, with headers       (browser -> bucket)
    3. your form posts the ticket; your view:

        key = uploads.finish(request.user, request.POST['vidlock_ticket'])
        seal_later(lesson, key)

``static/vidlock/upload.js`` does 1 and 2 for a plain Django form. The
ticket is signed, names one key, belongs to the user it was given to, and
works once; ``finish`` checks that the object is there and within
``MAX_UPLOAD_BYTES`` (a presigned PUT cannot limit the size itself, so an
oversize object is deleted then).

The bucket's CORS must allow ``PUT`` from your site with the
``Content-Type`` header — see the README.
"""

from __future__ import annotations

import os
import uuid

from django.core import signing
from django.core.cache import cache
from django.utils.translation import gettext as _

from vidlock import conf, packager
from vidlock.storage import default_storage

_SALT = 'vidlock.upload'
#: How long after its start an upload may still be finished: a big file on
#: a slow line takes a while, and the form may sit before it is submitted.
TICKET_MAX_AGE = 24 * 3600

CONTENT_TYPES = {'.mp4': 'video/mp4', '.m4v': 'video/x-m4v', '.mov': 'video/quicktime'}


class UploadRefused(Exception):
    """The upload cannot go ahead; ``str(exc)`` says why, for the uploader."""


def _limit() -> int | None:
    limit = conf.get('MAX_UPLOAD_BYTES')
    return int(limit) if limit else None


def _too_big(size: int) -> str:
    limit = _limit() or 0
    return _('The file is %(size)s MB; the limit is %(limit)s MB.') % {
        'size': max(1, size // 1024**2),
        'limit': limit // 1024**2,
    }


def start(user, name: str, size, storage=None) -> dict:
    """Sign an upload of the file ``name`` (``size`` bytes) for ``user``.

    Returns ``{'ticket', 'key', 'method', 'url', 'headers', 'expires_in'}``.
    Raises UploadRefused for a file vidlock cannot seal or one too big.
    """
    extension = os.path.splitext(name or '')[1].lower()
    if extension not in packager.SOURCE_EXTENSIONS:
        raise UploadRefused(_('Upload an MP4, M4V or MOV video.'))
    try:
        size = int(size)
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        raise UploadRefused(_('The file is empty.'))
    limit = _limit()
    if limit and size > limit:
        raise UploadRefused(_too_big(size))
    storage = storage or default_storage()
    key = f'{conf.get("UPLOAD_PREFIX") or ""}{uuid.uuid4().hex}{extension}'
    ttl = int(conf.get('UPLOAD_TTL'))
    # The content type comes from the extension, never from the browser.
    target = storage.upload_target(key, CONTENT_TYPES[extension], ttl)
    return {
        'ticket': signing.dumps({'k': key, 'u': user.pk}, salt=_SALT),
        'key': key,
        'method': 'PUT',
        'url': target['url'],
        'headers': target.get('headers', {}),
        'expires_in': ttl,
    }


def finish(user, ticket: str, storage=None) -> str:
    """The key ``ticket`` uploaded, once it is in the bucket. Raises
    UploadRefused for a forged, expired, foreign or reused ticket, a missing
    object, or one past ``MAX_UPLOAD_BYTES`` (which is then deleted)."""
    try:
        data = signing.loads(ticket or '', salt=_SALT, max_age=TICKET_MAX_AGE)
        key, owner = data['k'], data['u']
    except (signing.BadSignature, KeyError, TypeError):
        raise UploadRefused(_('This upload has expired. Choose the file again.')) from None
    if owner != user.pk:
        raise UploadRefused(_('This upload has expired. Choose the file again.'))
    done = f'vidlock:upload:done:{key}'
    if cache.get(done):
        raise UploadRefused(_('This upload has already been used.'))
    storage = storage or default_storage()
    size = storage.size(key)
    if not size:
        raise UploadRefused(_('The upload did not finish. Choose the file again.'))
    limit = _limit()
    if limit and size > limit:
        storage.delete(key)
        raise UploadRefused(_too_big(size))
    # One ticket, one video: a second form post with it is refused.
    if not cache.add(done, 1, TICKET_MAX_AGE):
        raise UploadRefused(_('This upload has already been used.'))
    return key
