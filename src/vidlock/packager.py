"""ffmpeg: turn an uploaded MP4 into one AES-128 encrypted MPEG-TS.

Remux only (``-c copy``). A 78 MB lesson packages in about a second with
under 50 MB of RAM, so this runs comfortably on a small shared server. The
price is one rendition — whatever was uploaded — and a source that must
already be H.264 (+ AAC/MP3 audio). Anything else is reported by ``probe``
and should be left as it was uploaded.

``-hls_flags single_file`` writes a single .ts plus a playlist of
BYTERANGEs into it; vidlock then encrypts each range on its own (AES-128-CBC,
as HLS expects), so a player can seek anywhere, and the whole video is one
object in storage: one upload, one signed URL, one delete.

**Key rotation.** Every ``KEY_ROTATION_SECONDS`` of video gets its own key
and IV, announced by an ``#EXT-X-KEY`` line before its first range. The key
view hands keys out no faster than a viewer could watch (``vidlock.guard``),
so a copied key opens a minute of a lesson, not the lesson, and a tool that
wants every key waits most of the video's length for them.
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


def _remux(binary: str, source: str, out_dir: str) -> str:
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
    return playlist_path


_BYTERANGE = re.compile(r'^#EXT-X-BYTERANGE:(\d+)(?:@(\d+))?$')


def _encrypt_range(key: bytes, iv: bytes, clear: bytes) -> bytes:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    padder = padding.PKCS7(128).padder()
    padded = padder.update(clear) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def key_uri(index: int) -> str:
    """The placeholder for key ``index`` in a stored playlist."""
    return KEY_PLACEHOLDER if index == 0 else f'{KEY_PLACEHOLDER}:{index}'


def package(source: str, workdir: str) -> tuple[str, str, bytes]:
    """Remux ``source`` into one encrypted .ts inside ``workdir``.

    Returns ``(ts_path, playlist, keys)``: ``keys`` is one 16-byte key per
    rotation period, concatenated. The playlist carries placeholders where the
    key URIs and the media URI go: both are per-viewer signed URLs, filled in
    at request time by ``vidlock.playlist.render``.
    """
    binary = ffmpeg_binary()
    if not binary:
        raise PackagingError('ffmpeg is not installed')

    clear_dir = os.path.join(workdir, 'clear')
    out_dir = os.path.join(workdir, 'out')
    os.makedirs(clear_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)
    with open(_remux(binary, source, clear_dir)) as fh:
        lines = fh.read().splitlines()
    clear_path = os.path.join(clear_dir, 'media.ts')
    if not os.path.isfile(clear_path) or '#EXT-X-ENDLIST' not in lines:
        raise PackagingError('ffmpeg produced an incomplete playlist')

    rotation = float(conf.get('KEY_ROTATION_SECONDS') or 0)
    ts_path = os.path.join(out_dir, 'media.ts')
    keys: list[bytes] = []
    out: list[str] = []
    period_start = 0.0  # media time where the current key began
    elapsed = 0.0
    offset = 0  # read position in the clear file
    written = 0
    pending_duration = None
    with open(clear_path, 'rb') as clear, open(ts_path, 'wb') as sealed:
        for line in lines:
            if line.startswith('#EXTINF:'):
                pending_duration = float(line[len('#EXTINF:') :].split(',')[0])
                if not keys or (rotation and elapsed - period_start >= rotation - 1e-6):
                    keys.append(secrets.token_bytes(16))
                    period_start = elapsed
                    out.append(
                        f'#EXT-X-KEY:METHOD=AES-128,URI="{key_uri(len(keys) - 1)}",'
                        f'IV=0x{secrets.token_hex(16)}'
                    )
                out.append(line)
                continue
            found = _BYTERANGE.match(line)
            if found:
                length = int(found.group(1))
                start = int(found.group(2)) if found.group(2) is not None else offset
                clear.seek(start)
                chunk = clear.read(length)
                if len(chunk) != length:
                    raise PackagingError('ffmpeg wrote a playlist longer than its media')
                iv = bytes.fromhex(out[_last_key_line(out)].rsplit('IV=0x', 1)[1])
                body = _encrypt_range(keys[-1], iv, chunk)
                sealed.write(body)
                out.append(f'#EXT-X-BYTERANGE:{len(body)}@{written}')
                written += len(body)
                offset = start + length
                continue
            if line.strip() == 'media.ts':
                out.append(MEDIA_PLACEHOLDER)
                elapsed += pending_duration or 0.0
                continue
            out.append(line)
    playlist = '\n'.join(out) + '\n'
    if not keys or MEDIA_PLACEHOLDER not in playlist:
        raise PackagingError('ffmpeg produced an incomplete playlist')
    return ts_path, playlist, b''.join(keys)


def _last_key_line(lines: list[str]) -> int:
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].startswith('#EXT-X-KEY:'):
            return index
    raise PackagingError('a range before any key')


def unpackage(ts_path: str, playlist: str, key: bytes, out_path: str) -> str:
    """Decrypt a sealed .ts back into a playable MP4 at ``out_path`` (remux, no
    re-encode). ``key`` is every key of the video, concatenated, as stored.
    The way back when you need the original: moving to another platform, or a
    KEK you are about to retire."""
    binary = ffmpeg_binary()
    if not binary:
        raise PackagingError('ffmpeg is not installed')
    key = bytes(key)
    workdir = os.path.dirname(os.path.abspath(out_path)) or '.'
    tag = secrets.token_hex(4)
    key_files = [os.path.join(workdir, f'.vidlock-{tag}-{i}.key') for i in range(len(key) // 16)]
    local = os.path.join(workdir, f'.vidlock-{tag}.m3u8')
    try:
        for index, path in enumerate(key_files):
            with open(path, 'wb') as fh:
                fh.write(key[index * 16 : index * 16 + 16])
        text = playlist.replace(MEDIA_PLACEHOLDER, ts_path)
        # Highest index first, so ':1' is not replaced inside ':10'.
        for index in range(len(key_files) - 1, 0, -1):
            text = text.replace(f'"{key_uri(index)}"', f'"{key_files[index]}"')
        text = text.replace(f'"{KEY_PLACEHOLDER}"', f'"{key_files[0]}"')
        with open(local, 'w') as fh:
            fh.write(text)
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
        for path in [*key_files, local]:
            if os.path.exists(path):
                os.remove(path)
    if run.returncode:
        raise PackagingError(f'ffmpeg exited {run.returncode}: {run.stderr.strip()[-300:]}')
    return out_path
