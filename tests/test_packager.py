import os
from unittest import mock

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from vidlock import packager
from vidlock.playlist import KEY_PLACEHOLDER, MEDIA_PLACEHOLDER, segment_count


def test_probe_names_the_codecs(sample):
    found = packager.probe(sample)
    assert tuple(found) == ('h264', 'aac')
    assert found.pix_fmt == 'yuv420p' and 24.5 < found.duration < 25.5
    assert found.problem == ''


def test_probe_falls_back_to_ffmpeg_without_ffprobe(sample, settings):
    settings.VIDLOCK = {**settings.VIDLOCK, 'FFPROBE_BINARY': 'no-such-ffprobe'}
    with mock.patch('vidlock.packager.ffprobe_binary', return_value=None):
        found = packager.probe(sample)
    assert (found.video, found.audio, found.pix_fmt) == ('h264', 'aac', 'yuv420p')
    assert 24.5 < found.duration < 25.5


def test_h264_that_browsers_cannot_decode_is_refused():
    assert packager.Probe('h264', 'aac', 'yuv420p10le', 'High 10').problem
    assert packager.Probe('h264', 'aac', 'yuv444p', 'High 4:4:4 Predictive').problem
    assert not packager.Probe('h264', 'aac', 'yuvj420p', 'Main').problem


def test_a_sealed_file_decrypts_back_to_a_playable_mp4(sample, tmp_path):
    work = tmp_path / 'work'
    work.mkdir()
    ts, playlist, key = packager.package(sample, str(work))
    out = packager.unpackage(ts, playlist, key, str(tmp_path / 'back.mp4'))
    found = packager.probe(out)
    assert tuple(found) == ('h264', 'aac') and 24 < found.duration < 26
    assert not [p for p in os.listdir(tmp_path) if p.startswith('.vidlock-')], 'no key left on disk'


def test_the_wrong_key_does_not_decrypt(sample, tmp_path):
    work = tmp_path / 'work'
    work.mkdir()
    ts, playlist, _key = packager.package(sample, str(work))
    with pytest.raises(packager.PackagingError):
        packager.unpackage(ts, playlist, b'x' * 16, str(tmp_path / 'back.mp4'))


def test_one_encrypted_file_whose_ranges_decrypt_on_their_own(sample, tmp_path):
    ts, playlist, key = packager.package(sample, str(tmp_path))

    assert len(key) == 16
    assert KEY_PLACEHOLDER in playlist and MEDIA_PLACEHOLDER in playlist
    assert '#EXT-X-ENDLIST' in playlist
    assert segment_count(playlist) == 3
    with open(ts, 'rb') as fh:
        data = fh.read()
    assert data[0] != 0x47, 'the stored file must not be a playable MPEG-TS'

    # The second range alone: a player seeking there needs nothing before it.
    iv = bytes.fromhex(playlist.split('IV=0x')[1][:32])
    second = [line for line in playlist.splitlines() if line.startswith('#EXT-X-BYTERANGE')][1]
    length, offset = (int(n) for n in second.split(':')[1].split('@'))
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    clear = decryptor.update(data[offset : offset + length]) + decryptor.finalize()
    assert clear[0] == 0x47 and clear[188] == 0x47


def test_codecs_that_need_a_re_encode_are_refused():
    assert packager.can_seal('h264', 'aac')
    assert packager.can_seal('h264', '')
    assert not packager.can_seal('hevc', 'aac')
    assert not packager.can_seal('h264', 'opus')
