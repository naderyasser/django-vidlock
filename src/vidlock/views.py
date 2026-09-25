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

from django.contrib.auth import get_user_model
from django.http import Http404, HttpResponse, HttpResponseForbidden
from django.urls import reverse
from django.views.decorators.http import require_GET

from vidlock import conf, guard, tokens
from vidlock.playlist import CONTENT_TYPE, render, with_token
from vidlock.storage import default_storage


def _backend():
    cls = conf.load('BACKEND')
    if cls is None:
        from django.core.exceptions import ImproperlyConfigured

        raise ImproperlyConfigured("VIDLOCK['BACKEND'] is not set")
    return cls()


def playback_info(request, video, storage=None, channel=None):
    """What the player needs, for a viewer you have already authorised."""
    storage = storage or default_storage()
    ttl = int(conf.get('TOKEN_TTL'))
    if not video.is_sealed:
        return {'format': 'mp4', 'url': storage.signed_url(video.video_key, ttl), 'expires_in': ttl}
    token = tokens.sign(video.pk, request.user.pk, channel or tokens.channel_for(request))
    return {
        'format': 'hls',
        'url': with_token(request.build_absolute_uri(reverse('vidlock:playlist', args=[video.pk])), token),
        # The bundled player swaps these into requests already in flight, so
        # renewing before expiry never reloads the stream mid-sentence.
        'media_url': storage.signed_url(video.video_key, ttl),
        'key_url': with_token(request.build_absolute_uri(reverse('vidlock:key', args=[video.pk])), token),
        'expires_in': ttl,
    }


def _viewer(request, video_id, backend, require_session):
    user_id, channel = tokens.verify(request.GET.get('t', ''), video_id)
    if not user_id:
        return None, None, HttpResponseForbidden('This link has expired. Reload the page.')
    if require_session and channel == tokens.WEB and str(getattr(request.user, 'pk', '')) != user_id:
        # A web token pasted into a download tool arrives without the cookie
        # of the viewer it was minted for.
        return None, None, HttpResponseForbidden('Forbidden')
    video = backend.get_video(video_id)
    user = get_user_model()._default_manager.filter(pk=user_id, is_active=True).first()
    if video is None or user is None or not video.is_sealed:
        raise Http404
    if not backend.can_watch(user, video):
        return None, None, HttpResponseForbidden('Forbidden')
    return video, user, None


def _private(response):
    response['Cache-Control'] = 'private, no-store'
    return response


@require_GET
def playlist_view(request, video_id):
    # No cookie demanded here: Safari's native player may fetch the playlist
    # without one, and a playlist without its key opens nothing.
    backend = _backend()
    video, _, refusal = _viewer(request, video_id, backend, require_session=False)
    if refusal:
        return refusal
    token = request.GET['t']
    storage = default_storage()
    body = render(
        video.sealed_playlist,
        media_url=storage.signed_url(video.video_key, int(conf.get('TOKEN_TTL'))),
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
    if not guard.key_fetch_allowed(user.pk, video.pk, namespace):
        if guard.first_report_today(user.pk, video.pk, namespace):
            report = conf.load('ON_KEY_ABUSE')
            if report is not None:
                report(request, user, video)
        return HttpResponse('Too many requests.', status=429)
    return _private(HttpResponse(bytes(video.sealed_key), content_type='application/octet-stream'))
