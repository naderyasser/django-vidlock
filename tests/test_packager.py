from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from vidlock import packager
from vidlock.playlist import KEY_PLACEHOLDER, MEDIA_PLACEHOLDER, segment_count


def test_probe_names_the_codecs(sample):
    assert packager.probe(sample) == ('h264', 'aac')


def test_one_encrypted_file_whose_ranges_decrypt_on_their_own(sample, tmp_path):
    ts, playlist, key = packager.package(sample, str(tmp_path))

    assert len(key) == 16
    assert KEY_PLACEHOLDER in playlist and MEDIA_PLACEHOLDER in playlist
    assert '#EXT-X-ENDLIST' in playlist
    assert segment_count(playlist) == 3
    data = open(ts, 'rb').read()
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
