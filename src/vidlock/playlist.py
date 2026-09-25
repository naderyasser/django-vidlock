"""The stored playlist and what each viewer receives."""

from urllib.parse import quote

KEY_PLACEHOLDER = '__VIDLOCK_KEY__'
MEDIA_PLACEHOLDER = '__VIDLOCK_MEDIA__'
CONTENT_TYPE = 'application/vnd.apple.mpegurl'


def render(playlist, media_url, key_url):
    """Fill one viewer's signed media URL and key URL into a stored playlist."""
    return playlist.replace(KEY_PLACEHOLDER, key_url).replace(MEDIA_PLACEHOLDER, media_url)


def segment_count(playlist):
    """How many ranges a full playback fetches — each one a GET on the bucket."""
    return playlist.count('#EXTINF')


def with_token(url, token):
    return f'{url}{"&" if "?" in url else "?"}t={quote(token, safe="")}'
