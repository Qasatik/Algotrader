"""RFC 6238 TOTP (Time-based One-Time Password) implementation.

Implemented with the Python standard library only (hmac/hashlib/base64)
so the bot has zero extra dependencies for 2FA. Compatible with Google
Authenticator, Authy, 1Password, etc.

Typical flow:
  1. ``secret = generate_secret()``  -> store in env / vault.
  2. User scans ``provisioning_uri(secret, "admin@bybit-bot")`` in their
     authenticator app (or enters the base32 secret manually).
  3. On each Telegram command, the user sends a 6-digit code; the bot calls
     ``verify(secret, code)`` before executing privileged actions.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time

# TOTP parameters (RFC 6238 defaults; matches Google Authenticator)
DIGITS = 6
PERIOD = 30  # seconds
ALGORITHM = hashlib.sha1


def generate_secret(num_bytes: int = 20) -> str:
    """Generate a fresh base32-encoded shared secret (160 bits by default)."""
    raw = secrets.token_bytes(num_bytes)
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def _decode_secret(secret: str) -> bytes:
    """Decode a base32 secret, tolerating missing padding & spaces/casing."""
    cleaned = secret.strip().replace(" ", "").upper()
    padding = "=" * (-len(cleaned) % 8)
    return base64.b32decode(cleaned + padding)


def hotp(secret: str, counter: int, digits: int = DIGITS) -> str:
    """Compute HOTP for a given counter (RFC 4226)."""
    key = _decode_secret(secret)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, ALGORITHM).digest()

    # Dynamic truncation
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    code = binary % (10 ** digits)
    return str(code).zfill(digits)


def totp(secret: str, at_time: int | None = None, digits: int = DIGITS) -> str:
    """Compute the current TOTP code for ``secret``."""
    t = int(at_time if at_time is not None else time.time())
    counter = t // PERIOD
    return hotp(secret, counter, digits)


def verify(secret: str, code: str, at_time: int | None = None,
           allowed_drift: int = 1) -> bool:
    """Verify a user-supplied code, allowing ``allowed_drift`` periods of skew.

    A drift of 1 accepts the previous, current, and next 30s windows, which
    matches most authenticator apps' behaviour.
    """
    if not code or not code.isdigit():
        return False
    t = int(at_time if at_time is not None else time.time())
    base_counter = t // PERIOD
    for offset in range(-allowed_drift, allowed_drift + 1):
        expected = hotp(secret, base_counter + offset)
        if hmac.compare_digest(expected, str(code).zfill(DIGITS)):
            return True
    return False


def provisioning_uri(secret: str, label: str = "admin", issuer: str = "BybitAlgoBot") -> str:
    """Return an ``otpauth://`` URI encodable into a QR code."""
    from urllib.parse import quote, urlencode
    params = urlencode({"secret": secret, "issuer": issuer, "digits": DIGITS, "period": PERIOD})
    return f"otpauth://totp/{quote(issuer)}:{quote(label)}?{params}"


if __name__ == "__main__":  # pragma: no cover - CLI helper
    import argparse

    p = argparse.ArgumentParser(description="TOTP / 2FA helper")
    p.add_argument("--generate", action="store_true", help="print a new base32 secret")
    p.add_argument("--secret", help="verify a code against this secret")
    p.add_argument("--code", help="the 6-digit code to verify")
    args = p.parse_args()

    if args.generate:
        s = generate_secret()
        print("Secret:", s)
        print("URI:   ", provisioning_uri(s))
    elif args.secret and args.code:
        print("VALID" if verify(args.secret, args.code) else "INVALID")
    else:
        p.print_help()
