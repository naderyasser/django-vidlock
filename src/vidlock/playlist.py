"""The stored playlist and what each viewer receives."""

from __future__ import annotations

import re
from urllib.parse import quote

KEY_PLACEHOLDER = '__VIDLOCK_KEY__'
MEDIA_PLACEHOLDER = '__VIDLOCK_MEDIA__'
CONTENT_TYPE = 'application/vnd.apple.mpegurl'


_EXTINF = re.compile(r'^#EXTINF:([0-9.]+)', re.MULTILINE)


_KEY = re.compile(re.escape(KEY_PLACEHOLDER) + r'(?::(\d+))?')


def render(playlist: str, media_url: str, key_url: str) -> str:
    """Fill one viewer's signed media URL and key URLs into a stored playlist.
    Key ``n`` of a rotated video becomes ``key_url`` plus ``k=n``."""

    def key(match):
        index = match.group(1)
        return with_param(key_url, 'k', index) if index else key_url

    return _KEY.sub(key, playlist).replace(MEDIA_PLACEHOLDER, media_url)


def key_count(playlist: str) -> int:
    """How many keys the playlist names (1 for a video sealed without rotation)."""
    return len(set(_KEY.findall(playlist))) or 1


def segment_count(playlist: str) -> int:
    """How many ranges a full playback fetches — each one a GET on the bucket."""
    return playlist.count('#EXTINF')


def duration(playlist: str) -> float:
    """Total seconds of media the playlist describes."""
    return round(sum(float(value) for value in _EXTINF.findall(playlist)), 3)


def with_param(url: str, name: str, value: str) -> str:
    return f'{url}{"&" if "?" in url else "?"}{name}={quote(str(value), safe="")}'


def with_token(url: str, token: str) -> str:
    return with_param(url, 't', token)
