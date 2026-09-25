"""ffmpeg: turn an uploaded MP4 into one AES-128 encrypted MPEG-TS.

Remux only (``-c copy``). A 78 MB lesson packages in about a second with
under 50 MB of RAM, so this runs comfortably on a small shared server. The
price is one rendition — whatever was uploaded — and a source that must
already be H.264 (+ AAC/MP3 audio). Anything else is reported by ``probe``
and should be left as it was uploaded.

``-hls_flags single_file`` writes a single .ts plus a playlist of
BYTERANGEs into it. Every range decrypts on its own (one IV per video,
written into the playlist), so a player can seek anywhere, and the whole
video is one object in storage: one upload, one signed URL, one delete.
"""

import os
import re
import secrets
import shutil
import subprocess

from vidlock import conf
from vidlock.playlist import KEY_PLACEHOLDER, MEDIA_PLACEHOLDER

#: Codecs MPEG-TS carries and every HLS player decodes, with no re-encode.
VIDEO_CODECS = frozenset({'h264'})
AUDIO_CODECS = frozenset({'aac', 'mp3'})
#: Containers worth probing. WebM/MKV hold VP8/VP9/AV1 far more often.
SOURCE_EXTENSIONS = frozenset({'.mp4', '.m4v', '.mov'})

TIMEOUT_SECONDS = 30 * 60


class PackagingError(RuntimeError):
    pass


def ffmpeg_binary():
    """Absolute path to ffmpeg, or None when it is not installed."""
    return shutil.which(conf.get('FFMPEG_BINARY') or 'ffmpeg')


def available():
    return ffmpeg_binary() is not None


def probe(path):
    """(video codec, audio codec) as ffmpeg names them; '' when absent."""
    binary = ffmpeg_binary()
    if not binary:
        raise PackagingError('ffmpeg is not installed')
    run = subprocess.run(
        [binary, '-hide_banner', '-nostdin', '-i', path],
        capture_output=True,
        text=True,
        timeout=120,
    )
    video = re.search(r'Stream #\S+.*?: Video: (\w+)', run.stderr)
    audio = re.search(r'Stream #\S+.*?: Audio: (\w+)', run.stderr)
    return (video.group(1) if video else '', audio.group(1) if audio else '')


def can_seal(video_codec, audio_codec):
    return video_codec in VIDEO_CODECS and (not audio_codec or audio_codec in AUDIO_CODECS)


def _lowest_priority():
    # Packaging is batch work; the web server's requests come first.
    os.nice(19)


def package(source, workdir):
    """Remux ``source`` into one encrypted .ts inside ``workdir``.

    Returns ``(ts_path, playlist, key)``. The playlist carries placeholders
    where the key URI and the media URI go: both are per-viewer signed URLs,
    filled in at request time by ``vidlock.playlist.render``.
    """
    binary = ffmpeg_binary()
    if not binary:
        raise PackagingError('ffmpeg is not installed')

    key = secrets.token_bytes(16)
    key_file = os.path.join(workdir, 'enc.key')
    with open(key_file, 'wb') as fh:
        fh.write(key)
    key_info = os.path.join(workdir, 'enc.keyinfo')
    with open(key_info, 'w') as fh:
        fh.write(f'{KEY_PLACEHOLDER}\n{key_file}\n{secrets.token_hex(16)}\n')

    out_dir = os.path.join(workdir, 'out')
    os.makedirs(out_dir, exist_ok=True)
    playlist_path = os.path.join(out_dir, 'media.m3u8')
    run = subprocess.run(
        [
            binary,
            '-hide_banner',
            '-loglevel',
            'error',
            '-nostdin',
            '-i',
            source,
            '-map',
            '0:v:0',
            '-map',
            '0:a:0?',
            '-c',
            'copy',
            '-f',
            'hls',
            '-hls_time',
            str(conf.get('SEGMENT_SECONDS')),
            '-hls_playlist_type',
            'vod',
            '-hls_flags',
            'single_file',
            '-hls_key_info_file',
            key_info,
            playlist_path,
        ],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        preexec_fn=_lowest_priority if hasattr(os, 'nice') else None,
    )
    if run.returncode:
        raise PackagingError(f'ffmpeg exited {run.returncode}: {run.stderr.strip()[-300:]}')

    # ffmpeg can leave media.ts.tmp beside the output; only the named file is
    # the finished one.
    ts_path = os.path.join(out_dir, 'media.ts')
    with open(playlist_path) as fh:
        lines = fh.read().splitlines()
    playlist = '\n'.join(MEDIA_PLACEHOLDER if line.strip() == 'media.ts' else line for line in lines) + '\n'
    if (
        not os.path.isfile(ts_path)
        or '#EXT-X-ENDLIST' not in playlist
        or KEY_PLACEHOLDER not in playlist
        or MEDIA_PLACEHOLDER not in playlist
    ):
        raise PackagingError('ffmpeg produced an incomplete playlist')
    return ts_path, playlist, key
