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

import json
import logging
import math
import time

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_GET, require_POST

from vidlock import (
    conf,
    guard,
    keys,
    monitor,
    progress,
    risk,
    signals,
    streams,
    tokens,
    trace,
    transcribe,
    uploads,
    wrap,
)
from vidlock.models import Transcript
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


def _namespace(backend, request) -> str:
    return backend.namespace(request) if backend is not None else ''


def playback_info(request, video, storage=None, channel=None) -> dict:
    """What the player needs, for a viewer you have already authorised."""
    storage = storage or default_storage()
    ttl = int(conf.get('TOKEN_TTL'))
    backend_cls = conf.load('BACKEND')
    backend = backend_cls() if backend_cls else None
    user = request.user
    namespace = _namespace(backend, request)
    if risk.is_suspended(user.pk, namespace) and monitor.would_refuse('suspended', request, user, video):
        return {'format': 'blocked', 'error': str(_SUSPENDED)}
    extra = {}
    text = backend.watermark(user, video) if backend is not None else None
    if text:
        extra['watermark'] = f'{text} · {trace.code_for(user)}' if conf.get('WATERMARK_CODE') else str(text)
    if not video.is_sealed:
        return {'format': 'mp4', 'url': storage.signed_url(video.video_key, ttl), 'expires_in': ttl, **extra}

    channel = channel or tokens.channel_for(request)
    lease = streams.lease_for(request, channel)
    # The bundled player adds vidlock_resume=1 to its own renewals: those keep
    # a stream, never take one from another device.
    outcome = streams.open_lease(user.pk, lease, namespace, take=request.GET.get('vidlock_resume') != '1')
    if outcome == streams.REFUSED and monitor.would_refuse('elsewhere', request, user, video):
        return {'format': 'elsewhere', 'error': str(_ELSEWHERE)}
    if outcome == streams.TOOK:
        risk.note(user, 'takeover', namespace, request)
    if backend is not None:
        risk.seen_from(user, backend.client_ip(request), namespace, request)
    risk.sweep(user, namespace, request)
    token = tokens.sign_for(request, video.pk, channel, lease)

    def url(name):
        return with_token(request.build_absolute_uri(reverse(name, args=[video.pk])), token)

    captions = Transcript.objects.filter(video_type=video._meta.label, video_id=str(video.pk)).values_list(
        'language', flat=True
    )
    if captions:
        extra['captions_url'] = url('vidlock:captions')
        extra['captions_language'] = captions[0] or 'und'
    return {
        'format': 'hls',
        'url': url('vidlock:playlist'),
        # The bundled player swaps these into requests already in flight, so
        # renewing before expiry never reloads the stream mid-sentence.
        'media_url': storage.signed_url(video.video_key, ttl),
        'key_url': url('vidlock:key'),
        'heartbeat_url': url('vidlock:heartbeat'),
        'heartbeat_interval': int(conf.get('HEARTBEAT_SECONDS')),
        'key_exchange': 'ecdh-p256-v1',
        'expires_in': ttl,
        'duration': video.sealed_duration,
        **extra,
    }


_EXPIRED = gettext_lazy('This link has expired. Reload the page.')
_SUSPENDED = gettext_lazy('Playback is paused on this account. Please contact support.')
_ELSEWHERE = gettext_lazy('This account is watching on another device.')


def _forbidden(message=None):
    return HttpResponseForbidden(message or _('Forbidden'))


def _fetch_metadata_refusal(request):
    """Browsers label every request with Sec-Fetch-* headers; a player's key
    request is a same-site fetch, never a page navigation."""
    site = request.headers.get('Sec-Fetch-Site')
    if site is None:
        return bool(conf.get('STRICT_FETCH_METADATA'))
    return site == 'cross-site' or request.headers.get('Sec-Fetch-Mode') == 'navigate'


class _Viewer:
    def __init__(self, video, user, claims, namespace):
        self.video, self.user, self.claims, self.namespace = video, user, claims, namespace


def _viewer(request, video_id, backend, require_session):
    """``(viewer, None)`` for a request this token may make, else ``(None, refusal)``."""
    found = tokens.claims(request.GET.get('t', ''), video_id)
    if found is None:
        return None, _forbidden(_EXPIRED)
    if require_session and found.channel == tokens.WEB:
        # A web token pasted into a download tool arrives without the cookie
        # of the viewer it was minted for, or from a session since logged out.
        same_viewer = str(getattr(request.user, 'pk', '')) == found.user_id
        if not same_viewer or not tokens.session_matches(found, request):
            return None, _forbidden()
    video = backend.get_video(video_id)
    user = get_user_model()._default_manager.filter(pk=found.user_id, is_active=True).first()
    if video is None or user is None or not video.is_sealed:
        raise Http404
    namespace = backend.namespace(request)
    if not tokens.account_matches(found, user):
        # The password changed since the token was issued.
        return None, _forbidden(_EXPIRED)
    if risk.is_suspended(user.pk, namespace) and monitor.would_refuse('suspended', request, user, video):
        return None, _forbidden(_SUSPENDED)
    if require_session and found.channel == tokens.WEB and _fetch_metadata_refusal(request):
        risk.note(user, 'fetch_metadata', namespace, request)
        if monitor.would_refuse('fetch_metadata', request, user, video):
            return None, _forbidden()
    if (
        found.lease
        and not streams.holds(user.pk, found.lease, namespace)
        and monitor.would_refuse('elsewhere', request, user, video)
    ):
        return None, HttpResponse(_ELSEWHERE, status=409)
    if not backend.can_watch(user, video):
        return None, _forbidden()
    return _Viewer(video, user, found, namespace), None


def _private(response):
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Referrer-Policy'] = 'no-referrer'
    return response


@require_GET
def playlist_view(request, video_id):
    # No cookie demanded here: Safari's native player may fetch the playlist
    # without one, and a playlist without its keys opens nothing.
    backend = _backend()
    viewer, refusal = _viewer(request, video_id, backend, require_session=False)
    if refusal:
        return refusal
    video = viewer.video
    token = request.GET['t']
    storage = default_storage()
    body = render(
        video.sealed_playlist,
        media_url=storage.signed_url(video.video_key, media_ttl(video)),
        key_url=with_token(request.build_absolute_uri(reverse('vidlock:key', args=[video.pk])), token),
    )
    return _private(HttpResponse(body, content_type=CONTENT_TYPE))


@require_GET
def captions_view(request, video_id):
    """The lesson's transcript as WebVTT, for the player's captions track.
    Same token as the playlist; the transcript is part of the lesson."""
    backend = _backend()
    viewer, refusal = _viewer(request, video_id, backend, require_session=False)
    if refusal:
        return refusal
    found = Transcript.objects.filter(
        video_type=viewer.video._meta.label, video_id=str(viewer.video.pk)
    ).first()
    if found is None:
        raise Http404
    return _private(HttpResponse(transcribe.webvtt(found), content_type='text/vtt; charset=utf-8'))


@require_GET
def key_view(request, video_id):
    backend = _backend()
    viewer, refusal = _viewer(request, video_id, backend, require_session=True)
    if refusal:
        return refusal
    video, user, found, namespace = viewer.video, viewer.user, viewer.claims, viewer.namespace
    try:
        index = int(request.GET.get('k', '0'))
    except ValueError:
        raise Http404 from None
    count = video.key_count
    if not 0 <= index < count:
        raise Http404

    share = request.headers.get(wrap.HEADER)
    if share:
        # Before the limits: a malformed request must not spend the viewer's keys.
        try:
            wrap.check_share(share)
        except wrap.WrapError:
            return HttpResponse(status=400)
    if found.channel == tokens.WEB and not share:
        risk.note(user, 'raw_key', namespace, request, once=f'{found.lease}|{video.pk}')
        if conf.get('REQUIRE_WRAPPED_KEY') and monitor.would_refuse('raw_key', request, user, video):
            return _forbidden()
    risk.seen_from(user, backend.client_ip(request), namespace, request)

    key_seconds = video.sealed_duration / count if count > 1 else None
    reason = guard.check_key_fetch(user.pk, video.pk, namespace, index, key_seconds)
    if reason:
        risk.note(user, reason, namespace, request)
        if guard.first_report_today(user.pk, video.pk, namespace, reason):
            logger.warning('vidlock: %s limit crossed by user %s on video %s', reason, user.pk, video.pk)
            signals.key_abuse.send(
                sender=type(backend), request=request, user=user, video=video, reason=reason
            )
            report = conf.load('ON_KEY_ABUSE')
            if report is not None:
                report(request, user, video)
        if monitor.would_refuse(reason, request, user, video):
            refused = HttpResponse(_('Too many requests.'), status=429)
            refused['Retry-After'] = str(guard.retry_after(key_seconds))
            return refused

    try:
        key = video.content_key(index)
    except keys.KeyUnwrapError:
        logger.error('vidlock: no configured KEY_ENCRYPTION_KEYS opens the key of video %s', video.pk)
        return HttpResponse(status=503)
    if found.channel == tokens.WEB:
        risk.keyed(user.pk, found.lease, video.pk, namespace)
        risk.sweep(user, namespace, request)
    if share:
        try:
            body = wrap.seal(share, key)
        except wrap.WrapError:
            return HttpResponse(status=400)
        return _private(HttpResponse(body, content_type=wrap.CONTENT_TYPE))
    return _private(HttpResponse(key, content_type='application/octet-stream'))


@csrf_exempt
@require_POST
def heartbeat_view(request, video_id):
    """The player, every HEARTBEAT_SECONDS while the page is open: keeps its
    stream lease, proves the key it got is being played, reports what was
    watched and any tampering it noticed. The token authenticates it (no CSRF
    cookie needed), with the same session binding as a key request."""
    found = tokens.claims(request.GET.get('t', ''), video_id)
    if found is None:
        return JsonResponse({'error': 'expired'}, status=403)
    if found.channel == tokens.WEB:
        same_viewer = str(getattr(request.user, 'pk', '')) == found.user_id
        if not same_viewer or not tokens.session_matches(found, request):
            return JsonResponse({'error': 'expired'}, status=403)
    user = get_user_model()._default_manager.filter(pk=found.user_id, is_active=True).first()
    if user is None or not tokens.account_matches(found, user):
        return JsonResponse({'error': 'expired'}, status=403)
    backend = _backend()
    namespace = backend.namespace(request)
    if risk.is_suspended(user.pk, namespace) and monitor.would_refuse('suspended', request, user):
        return JsonResponse({'error': 'suspended', 'message': str(_SUSPENDED)}, status=403)
    # A player beats every 30 s (and on pause); more than once a second from
    # one stream is a script.
    beat_key = f'vidlock:beat:{namespace}:{user.pk}:{found.lease or video_id}:{int(time.time())}'
    if not cache.add(beat_key, 1, 2):
        return JsonResponse({'error': 'slow down'}, status=429)
    video = backend.get_video(video_id)
    if video is None or not video.is_sealed or not backend.can_watch(user, video):
        return JsonResponse({'error': 'expired'}, status=403)
    if (
        found.lease
        and not streams.touch(user.pk, found.lease, namespace)
        and monitor.would_refuse('elsewhere', request, user, video)
    ):
        return JsonResponse({'error': 'elsewhere', 'message': str(_ELSEWHERE)}, status=409)
    try:
        report = json.loads(request.body or b'{}')
    except ValueError:
        report = {}
    report = report if isinstance(report, dict) else {}
    watched = report.get('watched') or []
    if report.get('playing') or watched:
        risk.played(user.pk, found.lease, video_id, namespace)
    if watched and conf.get('TRACK_PROGRESS'):
        progress.record(user, video, watched, report.get('position'), namespace)
    if report.get('tamper'):
        risk.note(user, 'tamper', namespace, request, once=found.lease or str(video_id))
    risk.stream_seen_from(user, found.lease, backend.client_ip(request), namespace, request)
    return JsonResponse({'ok': True, 'interval': int(conf.get('HEARTBEAT_SECONDS'))})


@csrf_protect
@require_POST
def upload_view(request):
    """Start a direct upload (``vidlock.uploads``): POST JSON ``{"name",
    "size"}``, get back where to PUT the file and the ticket to finish with.
    CSRF-protected even without the middleware; ``upload.js`` sends the token."""
    user = request.user
    if not user.is_authenticated:
        return JsonResponse({'error': str(_UPLOAD_SIGN_IN)}, status=403)
    backend_cls = conf.load('BACKEND')
    allowed = backend_cls().can_upload(user) if backend_cls else user.is_staff
    if not allowed:
        return JsonResponse({'error': str(_UPLOAD_FORBIDDEN)}, status=403)
    try:
        body = json.loads(request.body or b'{}')
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    try:
        return JsonResponse(uploads.start(user, str(body.get('name', '')), body.get('size')))
    except uploads.UploadRefused as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    except NotImplementedError:
        return JsonResponse({'error': 'This storage cannot take direct uploads.'}, status=501)


_UPLOAD_SIGN_IN = gettext_lazy('Sign in to upload.')
_UPLOAD_FORBIDDEN = gettext_lazy('You may not upload videos.')
