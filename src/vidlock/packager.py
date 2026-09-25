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

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
from dataclasses import dataclass

from vidlock import conf
from vidlock.playlist import KEY_PLACEHOLDER, MEDIA_PLACEHOLDER

#: Codecs MPEG-TS carries and every HLS player decodes, with no re-encode.
VIDEO_CODECS = frozenset({'h264'})
AUDIO_CODECS = frozenset({'aac', 'mp3'})
#: 8-bit 4:2:0 — what browsers' and phones' H.264 decoders accept. An H.264
#: file in 10-bit (High 10) or 4:2:2/4:4:4 plays in VLC and nowhere else.
PIXEL_FORMATS = frozenset({'yuv420p', 'yuvj420p'})
#: Containers worth probing. WebM/MKV hold VP8/VP9/AV1 far more often.
SOURCE_EXTENSIONS = frozenset({'.mp4', '.m4v', '.mov'})

TIMEOUT_SECONDS = 30 * 60


class PackagingError(RuntimeError):
    pass


@dataclass(frozen=True)
class Probe:
    """What a source holds, as ffprobe names it; '' when unknown or absent."""

    video: str = ''
    audio: str = ''
    pix_fmt: str = ''
    profile: str = ''
    duration: float = 0.0

    def __iter__(self):
        # ``video, audio = probe(path)`` keeps working as it did in 0.1.
        return iter((self.video, self.audio))

    @property
    def problem(self) -> str:
        """Why this source cannot be sealed without a re-encode; '' when it can."""
        if self.video not in VIDEO_CODECS:
            return f'{self.video or "?"}/{self.audio or "-"} needs a re-encode to H.264/AAC'
        if self.audio and self.audio not in AUDIO_CODECS:
            return f'{self.video}/{self.audio} needs a re-encode to H.264/AAC'
        if self.pix_fmt and self.pix_fmt not in PIXEL_FORMATS:
            return f'H.264 in {self.pix_fmt} ({self.profile or "?"}) needs a re-encode to 8-bit 4:2:0'
        return ''


def ffmpeg_binary() -> str | None:
    """Absolute path to ffmpeg, or None when it is not installed."""
    return shutil.which(conf.get('FFMPEG_BINARY') or 'ffmpeg')


def ffprobe_binary() -> str | None:
    """Absolute path to ffprobe: the configured one, the one beside ffmpeg,
    or the one on the PATH."""
    configured = conf.get('FFPROBE_BINARY')
    if configured:
        return shutil.which(configured)
    ffmpeg = ffmpeg_binary()
    if ffmpeg:
        folder, name = os.path.split(ffmpeg)
        sibling = shutil.which(os.path.join(folder, name.replace('ffmpeg', 'ffprobe')))
        if sibling and sibling != ffmpeg:
            return sibling
    return shutil.which('ffprobe')


def available() -> bool:
    return ffmpeg_binary() is not None


def probe(path: str) -> Probe:
    """What ``path`` holds. Uses ffprobe's JSON when ffprobe is installed, and
    falls back to reading ``ffmpeg -i`` when only ffmpeg is."""
    ffprobe = ffprobe_binary()
    if ffprobe:
        return _probe_json(ffprobe, path)
    binary = ffmpeg_binary()
    if not binary:
        raise PackagingError('ffmpeg is not installed')
    run = subprocess.run(
        [binary, '-hide_banner', '-nostdin', '-i', path],
        check=False,  # ffmpeg -i with no output always exits 1; stderr is the answer
        capture_output=True,
        text=True,
        timeout=120,
    )
    video = re.search(r'Stream #\S+.*?: Video: (\w+)(?: \(([^)]*)\))?[^,]*, (\w+)', run.stderr)
    audio = re.search(r'Stream #\S+.*?: Audio: (\w+)', run.stderr)
    length = re.search(r'Duration: (\d+):(\d+):([\d.]+)', run.stderr)
    return Probe(
        video=video.group(1) if video else '',
        audio=audio.group(1) if audio else '',
        profile=(video.group(2) or '') if video else '',
        pix_fmt=video.group(3) if video else '',
        duration=(
            (int(length.group(1)) * 60 + int(length.group(2))) * 60 + float(length.group(3))
            if length
            else 0.0
        ),
    )


def _probe_json(ffprobe: str, path: str) -> Probe:
    run = subprocess.run(
        [
            ffprobe,
            '-v',
            'error',
            '-show_entries',
            'stream=codec_type,codec_name,profile,pix_fmt:format=duration',
            '-of',
            'json',
            path,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    try:
        data = json.loads(run.stdout or '{}')
    except ValueError:
        data = {}
    streams = data.get('streams') or []
    video = next((s for s in streams if s.get('codec_type') == 'video'), {})
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), {})
    try:
        length = float((data.get('format') or {}).get('duration') or 0)
    except ValueError:
        length = 0.0
    return Probe(
        video=video.get('codec_name', ''),
        audio=audio.get('codec_name', ''),
        pix_fmt=video.get('pix_fmt', ''),
        profile=video.get('profile', ''),
        duration=length,
    )


def can_seal(video_codec: str | Probe, audio_codec: str = '') -> bool:
    """Whether a source can be sealed by remuxing alone. Takes a ``Probe``, or
    the two codec names as in 0.1."""
    found = video_codec if isinstance(video_codec, Probe) else Probe(video_codec, audio_codec)
    return not found.problem


def _lowest_priority():
    # Packaging is batch work; the web server's requests come first.
    os.nice(19)


def package(source: str, workdir: str) -> tuple[str, str, bytes]:
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
        check=False,  # the exit code is turned into a PackagingError with ffmpeg's own words
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


def unpackage(ts_path: str, playlist: str, key: bytes, out_path: str) -> str:
    """Decrypt a sealed .ts back into a playable MP4 at ``out_path`` (remux, no
    re-encode). The way back when you need the original: moving to another
    platform, or a KEK you are about to retire."""
    binary = ffmpeg_binary()
    if not binary:
        raise PackagingError('ffmpeg is not installed')
    workdir = os.path.dirname(os.path.abspath(out_path)) or '.'
    key_file = os.path.join(workdir, f'.vidlock-{secrets.token_hex(4)}.key')
    local = os.path.join(workdir, f'.vidlock-{secrets.token_hex(4)}.m3u8')
    try:
        with open(key_file, 'wb') as fh:
            fh.write(bytes(key))
        with open(local, 'w') as fh:
            fh.write(playlist.replace(KEY_PLACEHOLDER, key_file).replace(MEDIA_PLACEHOLDER, ts_path))
        run = subprocess.run(
            [
                binary,
                '-hide_banner',
                '-loglevel',
                'error',
                '-nostdin',
                '-y',
                '-allowed_extensions',
                'ALL',
                '-protocol_whitelist',
                'file,crypto',
                '-i',
                local,
                '-map',
                '0',
                '-c',
                'copy',
                '-movflags',
                '+faststart',
                out_path,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    finally:
        for path in (key_file, local):
            if os.path.exists(path):
                os.remove(path)
    if run.returncode:
        raise PackagingError(f'ffmpeg exited {run.returncode}: {run.stderr.strip()[-300:]}')
    return out_path
