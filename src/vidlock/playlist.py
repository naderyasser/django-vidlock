"""The stored playlist and what each viewer receives."""

from __future__ import annotations

import re
from urllib.parse import quote

KEY_PLACEHOLDER = '__VIDLOCK_KEY__'
MEDIA_PLACEHOLDER = '__VIDLOCK_MEDIA__'
CONTENT_TYPE = 'application/vnd.apple.mpegurl'


_EXTINF = re.compile(r'^#EXTINF:([0-9.]+)', re.MULTILINE)


def render(playlist: str, media_url: str, key_url: str) -> str:
    """Fill one viewer's signed media URL and key URL into a stored playlist."""
    return playlist.replace(KEY_PLACEHOLDER, key_url).replace(MEDIA_PLACEHOLDER, media_url)


def segment_count(playlist: str) -> int:
    """How many ranges a full playback fetches — each one a GET on the bucket."""
    return playlist.count('#EXTINF')


def duration(playlist: str) -> float:
    """Total seconds of media the playlist describes."""
    return round(sum(float(value) for value in _EXTINF.findall(playlist)), 3)


def with_token(url: str, token: str) -> str:
    return f'{url}{"&" if "?" in url else "?"}t={quote(token, safe="")}'
