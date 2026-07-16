"""AES-256-GCM encryption for user API keys (at-rest protection).

Security model
--------------
The **master secret** lives ONLY in the environment (``SAAS_MASTER_SECRET``),
never in the database or source code. From it we derive a 256-bit AES key via
HKDF-SHA256. Each encryption uses a **fresh random nonce** (prepended to the
ciphertext) so identical plaintexts produce different ciphertexts.

GCM is *authenticated* encryption: any tampering with the stored ciphertext is
detected on decrypt (raises ``InvalidTag``). This means a stolen DB without the
master secret is useless, and a corrupted/tampered key is caught rather than
silently decrypting to garbage that gets sent to Bybit.

Usage::

    secret = generate_master_secret()      # one-time, store in .env
    token  = encrypt("my-api-secret", secret)   # store token in DB
    plain  = decrypt(token, secret)             # decrypt at runtime
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# AES-256 key = 32 bytes; GCM nonce = 12 bytes (NIST-recommended).
_KEY_SIZE = 32
_NONCE_SIZE = 12
# Domain-separation label so this key can't be confused with other HKDF uses.
_HKDF_INFO = b"carry-saas-api-key-encryption-v1"


class DecryptionError(Exception):
    """Raised when a token cannot be decrypted (wrong key or tampered)."""


def generate_master_secret() -> str:
    """Generate a random master secret for initial setup.

    Store the result in ``.env`` as ``SAAS_MASTER_SECRET``. Losing it means
    losing access to ALL stored API keys — back it up securely.
    """
    return base64.urlsafe_b64encode(os.urandom(_KEY_SIZE)).decode()


def derive_key(master_secret: str) -> bytes:
    """Derive a 256-bit AES key from *master_secret* via HKDF-SHA256."""
    if not master_secret:
        raise ValueError("master_secret must not be empty")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_SIZE,
        salt=None,
        info=_HKDF_INFO,
    ).derive(master_secret.encode())


def encrypt(plaintext: str, master_secret: str) -> str:
    """Encrypt *plaintext* with AES-256-GCM.

    Returns ``base64(nonce ‖ ciphertext ‖ tag)`` — a single self-contained
    token safe to store in a TEXT database column.
    """
    key = derive_key(master_secret)
    nonce = os.urandom(_NONCE_SIZE)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
    return base64.urlsafe_b64encode(nonce + ct).decode()


def decrypt(token: str, master_secret: str) -> str:
    """Decrypt a token produced by :func:`encrypt`.

    Raises :class:`DecryptionError` if the key is wrong or the token was
    tampered with (GCM authentication tag mismatch).
    """
    key = derive_key(master_secret)
    try:
        raw = base64.urlsafe_b64decode(token.encode())
        nonce, ct = raw[:_NONCE_SIZE], raw[_NONCE_SIZE:]
        return AESGCM(key).decrypt(nonce, ct, None).decode()
    except (InvalidTag, ValueError, base64.binascii.Error) as exc:
        raise DecryptionError("failed to decrypt token (wrong key or tampered)") from exc
