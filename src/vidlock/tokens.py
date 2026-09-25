"""Per-viewer tokens for the playlist and the key.

The player fetches both itself: hls.js sends the site's cookie, but a phone's
native player (ExoPlayer, AVPlayer) sends nothing at all. So the token rides
in the URL and is bound to one viewer, one video and a few minutes.

The channel records who the token was issued to. A ``web`` token was minted
for a cookie session, so the key view also demands that same cookie: a web
URL pasted into yt-dlp or N_m3u8DL-RE arrives without it and is refused. An
``app`` token (issued to a Bearer-authenticated request) cannot demand a
cookie, and relies on the fetch limit in ``vidlock.guard`` instead.
"""

from django.core.signing import BadSignature, SignatureExpired, TimestampSigner

from vidlock import conf

WEB, APP = 'web', 'app'
_SALT = 'vidlock.token'


def sign(video_id, user_id, channel=WEB):
    if channel not in (WEB, APP):
        raise ValueError(f'unknown channel {channel!r}')
    return TimestampSigner(salt=_SALT).sign(f'{video_id}|{user_id}|{channel}')


def verify(token, video_id, max_age=None):
    """``(user_id, channel)`` the token was signed for, or ``(None, None)``."""
    max_age = conf.get('TOKEN_TTL') if max_age is None else max_age
    try:
        value = TimestampSigner(salt=_SALT).unsign(token or '', max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None, None
    video, user, channel = [*value.split('|'), '', ''][:3]
    if video != str(video_id) or not user or channel not in (WEB, APP):
        return None, None
    return user, channel


def channel_for(request):
    """``app`` when a token authenticated the request (DRF / SimpleJWT set
    ``request.auth``), ``web`` for a cookie session."""
    return APP if getattr(request, 'auth', None) is not None else WEB
