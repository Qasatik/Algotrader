"""Tests for the SaaS API-key encryption module (AES-256-GCM)."""

import base64

import pytest

from saas import crypto

# ----------------------------------------------------------------- basic roundtrip


def test_encrypt_decrypt_roundtrip():
    secret = crypto.generate_master_secret()
    token = crypto.encrypt("super-secret-api-key-123", secret)
    assert crypto.decrypt(token, secret) == "super-secret-api-key-123"


def test_plaintext_not_in_token():
    """The ciphertext must NOT contain the plaintext in readable form."""
    secret = crypto.generate_master_secret()
    plaintext = "VERY_SECRET_KEY_DO_NOT_LEAK"
    token = crypto.encrypt(plaintext, secret)
    assert plaintext not in token
    assert plaintext not in base64.urlsafe_b64decode(token).decode("latin-1")


# ----------------------------------------------------------------- nonce randomness


def test_same_plaintext_different_ciphertext():
    """A fresh nonce every call → identical plaintexts encrypt differently."""
    secret = crypto.generate_master_secret()
    t1 = crypto.encrypt("same-key", secret)
    t2 = crypto.encrypt("same-key", secret)
    assert t1 != t2  # different nonces
    # ... but both decrypt to the same value
    assert crypto.decrypt(t1, secret) == "same-key"
    assert crypto.decrypt(t2, secret) == "same-key"


# ----------------------------------------------------------------- key separation


def test_wrong_master_secret_fails():
    secret_a = crypto.generate_master_secret()
    secret_b = crypto.generate_master_secret()
    token = crypto.encrypt("my-key", secret_a)
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(token, secret_b)


def test_empty_secret_raises():
    with pytest.raises(ValueError):
        crypto.encrypt("x", "")


# ----------------------------------------------------------------- tamper detection


def test_tampered_ciphertext_detected():
    """GCM authentication: flipping a byte must cause decryption to fail."""
    secret = crypto.generate_master_secret()
    token = crypto.encrypt("api-secret", secret)
    raw = bytearray(base64.urlsafe_b64decode(token))
    raw[-1] ^= 0xFF  # flip last byte (part of the auth tag / ciphertext)
    tampered = base64.urlsafe_b64encode(bytes(raw)).decode()
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(tampered, secret)


def test_corrupt_base64_detected():
    secret = crypto.generate_master_secret()
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt("not-valid-base64!!!", secret)


# ----------------------------------------------------------------- master secret


def test_generated_secret_is_url_safe_base64():
    s = crypto.generate_master_secret()
    # round-trips cleanly through urlsafe base64
    assert base64.urlsafe_b64decode(s) is not None
    assert len(s) >= 40  # 32 bytes → ~43 chars


def test_generated_secrets_are_unique():
    secrets = {crypto.generate_master_secret() for _ in range(20)}
    assert len(secrets) == 20
