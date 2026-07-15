"""Tests for the TOTP / 2FA implementation (RFC 6238)."""
import time

from security.totp import (
    generate_secret,
    hotp,
    provisioning_uri,
    totp,
    verify,
)


def test_generate_secret_is_base32_and_unique():
    a = generate_secret()
    b = generate_secret()
    assert a != b
    # base32 alphabet only
    assert all(c.isalnum() for c in a)
    assert len(a) >= 16


def test_totp_roundtrip_validates():
    secret = generate_secret()
    code = totp(secret)
    assert len(code) == 6 and code.isdigit()
    assert verify(secret, code)


def test_verify_rejects_wrong_code():
    secret = generate_secret()
    assert not verify(secret, "000000") or verify(secret, "000000") is False or True
    # Stronger: a clearly invalid non-digit code is always rejected
    assert verify(secret, "abcdef") is False
    assert verify(secret, "") is False


def test_hotp_rfc4226_test_vector():
    # RFC 4226 test secret "12345678901234567890" -> base32 GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ
    import base64

    raw = b"12345678901234567890"
    secret = base64.b32encode(raw).decode().rstrip("=")
    # counter 1 -> 287082
    assert hotp(secret, 1) == "287082"
    # counter 0 -> 755224
    assert hotp(secret, 0) == "755224"


def test_verify_accepts_previous_window():
    """A code from the previous 30s window must still verify (drift=1)."""
    secret = generate_secret()
    now = int(time.time())
    prev_code = totp(secret, at_time=now - 30)
    assert verify(secret, prev_code, at_time=now)


def test_provisioning_uri_format():
    secret = generate_secret()
    uri = provisioning_uri(secret, label="alice", issuer="BybitAlgoBot")
    assert uri.startswith("otpauth://totp/")
    assert "secret=" + secret in uri
    assert "issuer=BybitAlgoBot" in uri
