"""Handing the key to one page alone.

The bundled player makes a fresh ECDH P-256 key pair per page (its private
half is a non-extractable WebCrypto key) and sends the public half with every
key request. The key view answers with the content key sealed to it:

    server public key (65) | nonce (12) | AES-GCM(content key) (16 + 16 tag)

under a key derived by HKDF-SHA256 from the ECDH secret. What DevTools shows
is useless to a downloader: opening it takes the page's private key, which
never leaves the browser's crypto engine. (Someone who breakpoints the page's
JavaScript still gets the key — this raises the bar from copy-paste to
reverse engineering, and the risk score watches the rest.)
"""

from __future__ import annotations

import base64
import os

HEADER = 'X-Vidlock-Key-Share'
CONTENT_TYPE = 'application/vnd.vidlock.wrapped-key'
SALT = b'vidlock.wrap.v1'
INFO = b'vidlock content key'


class WrapError(ValueError):
    pass


def _b64decode(value: str) -> bytes:
    value = value.strip()
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def seal(client_public: str, content_key: bytes) -> bytes:
    """``content_key`` sealed to the page whose public key (raw, uncompressed
    P-256, base64url) is ``client_public``."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    try:
        raw = _b64decode(client_public)
        if len(raw) != 65 or raw[0] != 4:
            raise ValueError('not an uncompressed P-256 point')
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except Exception as exc:
        raise WrapError(f'bad {HEADER}: {exc}') from exc
    ours = ec.generate_private_key(ec.SECP256R1())
    shared = ours.exchange(ec.ECDH(), peer)
    wrap_key = HKDF(algorithm=hashes.SHA256(), length=16, salt=SALT, info=INFO).derive(shared)
    nonce = os.urandom(12)
    public = ours.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return public + nonce + AESGCM(wrap_key).encrypt(nonce, bytes(content_key), None)
