"""Per-viewer tokens for the playlist and the key.

The player fetches both itself: hls.js sends the site's cookie, but a phone's
native player (ExoPlayer, AVPlayer) sends nothing at all. So the token rides
in the URL and is bound to one viewer, one video and a few minutes.

The channel records who the token was issued to. A ``web`` token was minted
for a cookie session, so the key view also demands that same session: a web
URL pasted into yt-dlp or N_m3u8DL-RE arrives without it and is refused, and
logging out ends every token the session was given. An ``app`` token (issued
to a Bearer-authenticated request) cannot demand a cookie, and relies on the
fetch limits in ``vidlock.guard`` instead.

Every token also carries a fingerprint of the viewer's password hash, so a
password change (Django's "log out everywhere") voids tokens on both channels.
"""

from __future__ import annotations

from typing import NamedTuple

from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.utils.crypto import constant_time_compare, salted_hmac

from vidlock import conf

WEB, APP = 'web', 'app'
_SALT = 'vidlock.token'


class Claims(NamedTuple):
    user_id: str
    channel: str
    account: str  # fingerprint of the password hash; '' when not bound
    session: str  # fingerprint of the session key (web only); '' when not bound
    lease: str = ''  # the device's stream lease (vidlock.streams); '' when none


def _fingerprint(kind: str, value: str | None) -> str:
    if not value:
        return ''
    return salted_hmac(f'vidlock.fp.{kind}', value).hexdigest()[:16]


def account_fingerprint(user) -> str:
    get_hash = getattr(user, 'get_session_auth_hash', None)
    return _fingerprint('account', get_hash()) if callable(get_hash) else ''


def session_fingerprint(request) -> str:
    session = getattr(request, 'session', None)
    return _fingerprint('session', getattr(session, 'session_key', None))


def sign(video_id, user_id, channel: str = WEB, account: str = '', session: str = '', lease: str = '') -> str:
    if channel not in (WEB, APP):
        raise ValueError(f'unknown channel {channel!r}')
    return TimestampSigner(salt=_SALT).sign(f'{video_id}|{user_id}|{channel}|{account}|{session}|{lease}')


def sign_for(request, video_id, channel: str | None = None, lease: str = '') -> str:
    """A token for the viewer making ``request``, bound to their account, to
    the device's stream ``lease`` and, on the web channel, to this session."""
    channel = channel or channel_for(request)
    user = request.user
    session = session_fingerprint(request) if channel == WEB else ''
    return sign(video_id, user.pk, channel, account_fingerprint(user), session, lease)


def claims(token: str | None, video_id, max_age: int | None = None) -> Claims | None:
    """What a valid token for ``video_id`` says, or None."""
    max_age = conf.get('TOKEN_TTL') if max_age is None else max_age
    try:
        value = TimestampSigner(salt=_SALT).unsign(token or '', max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    video, user, channel, account, session, lease = [*value.split('|'), '', '', '', '', ''][:6]
    if video != str(video_id) or not user or channel not in (WEB, APP):
        return None
    return Claims(user, channel, account, session, lease)


def verify(token: str | None, video_id, max_age: int | None = None):
    """``(user_id, channel)`` the token was signed for, or ``(None, None)``."""
    found = claims(token, video_id, max_age)
    return (found.user_id, found.channel) if found else (None, None)


def account_matches(found: Claims, user) -> bool:
    return not found.account or constant_time_compare(found.account, account_fingerprint(user))


def session_matches(found: Claims, request) -> bool:
    return not found.session or constant_time_compare(found.session, session_fingerprint(request))


def channel_for(request) -> str:
    """``app`` when a token authenticated the request (DRF / SimpleJWT set
    ``request.auth``), ``web`` for a cookie session."""
    return APP if getattr(request, 'auth', None) is not None else WEB
