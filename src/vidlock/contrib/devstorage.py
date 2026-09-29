"""Storage for development: a folder on disk, served by Django with signed,
expiring URLs that answer Range requests like a bucket does.

    # settings.py (DEBUG only)
    VIDLOCK = {..., 'STORAGE': 'vidlock.contrib.devstorage.DevStorage'}
    # urls.py
    path('dev-media/', include('vidlock.contrib.devstorage')),

Files go to ``VIDLOCK['DEV_STORAGE_ROOT']`` (default: ``MEDIA_ROOT/vidlock``).
It refuses to work with DEBUG off: Django serving video is fine on a laptop
and wrong in production, where a bucket and its CDN should do it.
"""

from __future__ import annotations

import os
import re
import shutil

from django.conf import settings
from django.core import signing
from django.core.exceptions import ImproperlyConfigured
from django.http import FileResponse, Http404, HttpResponse, StreamingHttpResponse
from django.urls import path, reverse

from vidlock.storage import Storage

_SALT = 'vidlock.devstorage'
_RANGE = re.compile(r'^bytes=(\d+)-(\d*)$')
CHUNK = 256 * 1024


def _root() -> str:
    configured = getattr(settings, 'VIDLOCK', {}).get('DEV_STORAGE_ROOT')
    return str(configured or os.path.join(str(settings.MEDIA_ROOT or '.'), 'vidlock'))


def _require_debug():
    if not settings.DEBUG:
        raise ImproperlyConfigured('vidlock.contrib.devstorage is for development (DEBUG = True) only.')


def _path(key: str) -> str:
    root = os.path.realpath(_root())
    full = os.path.realpath(os.path.join(root, key))
    if not full.startswith(root + os.sep):
        raise Http404
    return full


class DevStorage(Storage):
    def __init__(self):
        _require_debug()

    def download(self, key, path):
        shutil.copyfile(_path(key), path)

    def upload(self, path, key, content_type):
        target = _path(key)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(path, target)
        return key

    def signed_url(self, key, ttl):
        # URL-safe base64 inside: object keys have slashes, a URL segment cannot.
        token = signing.dumps({'k': key, 'ttl': int(ttl)}, salt=_SALT)
        return reverse('vidlock-dev:media', args=[token])

    def delete(self, key):
        try:
            os.remove(_path(key))
            return True
        except (OSError, Http404):
            return False

    def save_upload(self, uploaded, key: str) -> str:
        """Store a Django ``UploadedFile`` (from a form) under ``key``."""
        target = _path(key)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'wb') as fh:
            for chunk in uploaded.chunks():
                fh.write(chunk)
        return key


def _ranged(full, start, length):
    with open(full, 'rb') as fh:
        fh.seek(start)
        left = length
        while left > 0:
            chunk = fh.read(min(CHUNK, left))
            if not chunk:
                break
            left -= len(chunk)
            yield chunk


def media(request, token):
    _require_debug()
    try:
        ttl = int(signing.loads(token, salt=_SALT)['ttl'])
        key = signing.loads(token, salt=_SALT, max_age=ttl)['k']
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        return HttpResponse('Expired or invalid link.', status=403)
    full = _path(key)
    if not os.path.isfile(full):
        raise Http404
    size = os.path.getsize(full)
    found = _RANGE.match(request.headers.get('Range', ''))
    if found:
        start = int(found.group(1))
        end = min(int(found.group(2)) if found.group(2) else size - 1, size - 1)
        if start > end:
            response = HttpResponse(status=416)
            response['Content-Range'] = f'bytes */{size}'
            return response
        response = StreamingHttpResponse(
            _ranged(full, start, end - start + 1), status=206, content_type='video/mp2t'
        )
        response['Content-Range'] = f'bytes {start}-{end}/{size}'
        response['Content-Length'] = str(end - start + 1)
    else:
        response = FileResponse(
            open(full, 'rb'),  # noqa: SIM115 — FileResponse closes it
            content_type='video/mp4' if full.endswith('.mp4') else 'video/mp2t',
        )
    response['Accept-Ranges'] = 'bytes'
    response['Cache-Control'] = 'private, no-store'
    return response


app_name = 'vidlock-dev'
urlpatterns = [path('<str:token>', media, name='media')]
