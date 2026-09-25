"""The playlist and key endpoints, plus the helper for your playback endpoint.

Your own view decides who may watch and then calls ``playback_info``:

    @login_required
    def playback(request, pk):
        lesson = get_object_or_404(Lesson, pk=pk)
        if not can_watch(request.user, lesson):
            return HttpResponseForbidden()
        return JsonResponse(playback_info(request, lesson))

The player reads ``format``: ``hls`` means hand ``url`` to hls.js (or to a
native player on Safari, iOS and Android); ``mp4`` means the video is not
sealed yet and ``url`` is the plain signed file.
"""

from __future__ import annotations

import logging
import math

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.http import Http404, HttpResponse, HttpResponseForbidden
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET

from vidlock import conf, guard, keys, signals, tokens
from vidlock.playlist import CONTENT_TYPE, duration, render, with_token
from vidlock.storage import MAX_SIGNED_TTL, default_storage

logger = logging.getLogger(__name__)


def _backend():
    cls = conf.load('BACKEND')
    if cls is None:
        raise ImproperlyConfigured("VIDLOCK['BACKEND'] is not set")
    return cls()


def media_ttl(video) -> int:
    """How long the media URL written into a playlist lives.

    hls.js swaps in fresh URLs as it goes, but Safari's and the phones' native
    players read the playlist once and fetch ranges from it until the end. So
    that URL outlives the whole video (plus a token's worth of pausing). It
    opens only the encrypted file; the key stays on its short leash.
    """
    ttl = int(conf.get('TOKEN_TTL'))
    length = duration(video.sealed_playlist) if video.sealed_playlist else 0
    return min(MAX_SIGNED_TTL, max(ttl, math.ceil(length) + ttl))


def playback_info(request, video, storage=None, channel=None) -> dict:
    """What the player needs, for a viewer you have already authorised."""
    storage = storage or default_storage()
    ttl = int(conf.get('TOKEN_TTL'))
    backend_cls = conf.load('BACKEND')
    watermark = backend_cls().watermark(request.user, video) if backend_cls else None
    extra = {'watermark': str(watermark)} if watermark else {}
    if not video.is_sealed:
        return {'format': 'mp4', 'url': storage.signed_url(video.video_key, ttl), 'expires_in': ttl, **extra}
    token = tokens.sign_for(request, video.pk, channel)
    return {
        'format': 'hls',
        'url': with_token(request.build_absolute_uri(reverse('vidlock:playlist', args=[video.pk])), token),
        # The bundled player swaps these into requests already in flight, so
        # renewing before expiry never reloads the stream mid-sentence.
        'media_url': storage.signed_url(video.video_key, ttl),
        'key_url': with_token(request.build_absolute_uri(reverse('vidlock:key', args=[video.pk])), token),
        'expires_in': ttl,
        'duration': video.sealed_duration,
        **extra,
    }


def _forbidden(message=None):
    return HttpResponseForbidden(message or _('Forbidden'))


def _fetch_metadata_refusal(request):
    """Browsers label every request with Sec-Fetch-* headers; a player's key
    request is a same-site fetch, never a page navigation."""
    site = request.headers.get('Sec-Fetch-Site')
    if site is None:
        return bool(conf.get('STRICT_FETCH_METADATA'))
    return site == 'cross-site' or request.headers.get('Sec-Fetch-Mode') == 'navigate'


def _viewer(request, video_id, backend, require_session):
    found = tokens.claims(request.GET.get('t', ''), video_id)
    if found is None:
        return None, None, _forbidden(_('This link has expired. Reload the page.'))
    if require_session and found.channel == tokens.WEB:
        # A web token pasted into a download tool arrives without the cookie
        # of the viewer it was minted for, or from a session since logged out.
        same_viewer = str(getattr(request.user, 'pk', '')) == found.user_id
        if not same_viewer or not tokens.session_matches(found, request):
            return None, None, _forbidden()
        if _fetch_metadata_refusal(request):
            return None, None, _forbidden()
    video = backend.get_video(video_id)
    user = get_user_model()._default_manager.filter(pk=found.user_id, is_active=True).first()
    if video is None or user is None or not video.is_sealed:
        raise Http404
    if not tokens.account_matches(found, user):
        # The password changed since the token was issued.
        return None, None, _forbidden(_('This link has expired. Reload the page.'))
    if not backend.can_watch(user, video):
        return None, None, _forbidden()
    return video, user, None


def _private(response):
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Referrer-Policy'] = 'no-referrer'
    return response


@require_GET
def playlist_view(request, video_id):
    # No cookie demanded here: Safari's native player may fetch the playlist
    # without one, and a playlist without its key opens nothing.
    backend = _backend()
    video, _user, refusal = _viewer(request, video_id, backend, require_session=False)
    if refusal:
        return refusal
    token = request.GET['t']
    storage = default_storage()
    body = render(
        video.sealed_playlist,
        media_url=storage.signed_url(video.video_key, media_ttl(video)),
        key_url=with_token(request.build_absolute_uri(reverse('vidlock:key', args=[video.pk])), token),
    )
    return _private(HttpResponse(body, content_type=CONTENT_TYPE))


@require_GET
def key_view(request, video_id):
    backend = _backend()
    video, user, refusal = _viewer(request, video_id, backend, require_session=True)
    if refusal:
        return refusal
    namespace = backend.namespace(request)
    reason = guard.check_key_fetch(user.pk, video.pk, namespace)
    if reason:
        if guard.first_report_today(user.pk, video.pk, namespace, reason):
            logger.warning('vidlock: %s limit crossed by user %s on video %s', reason, user.pk, video.pk)
            signals.key_abuse.send(
                sender=type(backend), request=request, user=user, video=video, reason=reason
            )
            report = conf.load('ON_KEY_ABUSE')
            if report is not None:
                report(request, user, video)
        return HttpResponse(_('Too many requests.'), status=429)
    try:
        key = keys.unwrap(video.sealed_key)
    except keys.KeyUnwrapError:
        logger.error('vidlock: no configured KEY_ENCRYPTION_KEYS opens the key of video %s', video.pk)
        return HttpResponse(status=503)
    return _private(HttpResponse(key, content_type='application/octet-stream'))
