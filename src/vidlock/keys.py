"""The per-video key, encrypted at rest.

The 16-byte AES key that opens a sealed video lives in your database. Stored
as is, a leaked database dump plus the bucket would be every course in the
clear. So the key is wrapped under a key-encryption key (KEK) that lives in
your settings, not in the database: the dump alone opens nothing.

Standard library only: HMAC-SHA256 serves as the PRF in counter mode with a
fresh random nonce, and a second HMAC authenticates the result
(encrypt-then-MAC, separate subkeys). A video with key rotation stores one
16-byte key per rotation period, all wrapped together in one blob.

Blob layout: ``VLK1 | kek id (4) | nonce (16) | ciphertext | tag (16)``.
A value without the ``VLK1`` prefix is a raw key written by vidlock 0.1 and is
still served; ``manage.py vidlock_rewrap`` encrypts it.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import NamedTuple

from django.conf import settings

from vidlock import conf

MAGIC = b'VLK1'
_NONCE, _TAG, _ID = 16, 16, 4


class KeyUnwrapError(ValueError):
    """No configured KEK opens this blob: a KEK was removed too soon, or the
    blob was tampered with."""


class _Kek(NamedTuple):
    ident: bytes
    enc: bytes
    mac: bytes


def _derive(secret: str | bytes) -> _Kek:
    raw = secret.encode() if isinstance(secret, str) else bytes(secret)

    def sub(label: bytes) -> bytes:
        return hmac.new(raw, b'vidlock.keys.' + label, hashlib.sha256).digest()

    return _Kek(sub(b'id')[:_ID], sub(b'enc'), sub(b'mac'))


def _keks() -> list[_Kek]:
    secrets_ = list(conf.get('KEY_ENCRYPTION_KEYS') or [])
    if not secrets_:
        secrets_ = [settings.SECRET_KEY, *getattr(settings, 'SECRET_KEY_FALLBACKS', [])]
    return [_derive(s) for s in secrets_ if s]


def _stream(kek: _Kek, nonce: bytes, length: int) -> bytes:
    # Block 0 is HMAC(nonce) alone, as in 0.2, so blobs of up to 32 bytes
    # written then still open; later blocks add a counter.
    blocks = [hmac.new(kek.enc, nonce, hashlib.sha256).digest()]
    counter = 1
    while len(blocks) * 32 < length:
        blocks.append(hmac.new(kek.enc, nonce + counter.to_bytes(4, 'big'), hashlib.sha256).digest())
        counter += 1
    return b''.join(blocks)[:length]


def _tag(kek: _Kek, head: bytes, body: bytes) -> bytes:
    return hmac.new(kek.mac, head + body, hashlib.sha256).digest()[:_TAG]


def wrap(key: bytes) -> bytes:
    """Encrypt a content key (or several, concatenated) under the current KEK."""
    key = bytes(key)
    if not 0 < len(key) <= 64 * 1024:
        raise ValueError('content keys are 1 byte to 64 KiB in all')
    kek = _keks()[0]
    nonce = secrets.token_bytes(_NONCE)
    body = bytes(a ^ b for a, b in zip(key, _stream(kek, nonce, len(key)), strict=True))
    head = MAGIC + kek.ident + nonce
    return head + body + _tag(kek, head, body)


_HEAD = len(MAGIC) + _ID + _NONCE


def is_wrapped(blob: bytes | memoryview | None) -> bool:
    # A wrapped blob is at least 41 bytes; a raw 16-byte key that happens to
    # start with the magic is still a raw key.
    return blob is not None and len(blob) > _HEAD + _TAG and bytes(blob[: len(MAGIC)]) == MAGIC


def unwrap(blob: bytes | memoryview) -> bytes:
    """The content key inside ``blob`` (a legacy raw key comes back as is)."""
    blob = bytes(blob)
    if not is_wrapped(blob):
        return blob
    head, body, tag = blob[:_HEAD], blob[_HEAD:-_TAG], blob[-_TAG:]
    ident, nonce = head[len(MAGIC) : len(MAGIC) + _ID], head[len(MAGIC) + _ID :]
    keks = _keks()
    # The KEK the blob names first; the rest cover a (4-byte) id collision.
    for kek in sorted(keks, key=lambda k: k.ident != ident):
        if hmac.compare_digest(_tag(kek, head, body), tag):
            return bytes(a ^ b for a, b in zip(body, _stream(kek, nonce, len(body)), strict=True))
    raise KeyUnwrapError('no key in VIDLOCK["KEY_ENCRYPTION_KEYS"] opens this video key')


def needs_rewrap(blob: bytes | memoryview | None) -> bool:
    """True for a raw key, or one wrapped under a KEK that is no longer first."""
    if not blob:
        return False
    if not is_wrapped(blob):
        return True
    return bytes(blob[len(MAGIC) : len(MAGIC) + _ID]) != _keks()[0].ident
