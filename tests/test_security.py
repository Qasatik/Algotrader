"""Tests for the API-key security audit (P3-13)."""

from __future__ import annotations

from unittest.mock import MagicMock

from core.security import ApiKeyAudit, audit_api_key, format_audit


def _safe_info(**overrides):
    """A clean, trade-only API key info dict."""
    base = {
        "note": "alg",
        "readOnly": 0,
        "permissions": {
            "ContractTrade": [],
            "Spot": ["SpotTrade"],
            "Wallet": [],
            "Derivatives": ["DerivativesTrade"],
            "Options": [],
        },
        "ips": ["*"],
        "expiredAt": "2099-01-01T00:00:00Z",
    }
    base.update(overrides)
    return base


def test_safe_trade_only_key_passes():
    ex = MagicMock()
    ex.get_api_key_info.return_value = _safe_info()
    a = audit_api_key(ex)
    assert a.ok is True
    assert a.note == "alg"
    assert a.error is None


def test_wallet_permission_blocks():
    ex = MagicMock()
    ex.get_api_key_info.return_value = _safe_info(
        permissions={"Spot": ["SpotTrade"], "Wallet": ["AccountTransfer"]}
    )
    a = audit_api_key(ex)
    assert a.ok is False
    assert any("Wallet" in w for w in a.warnings)


def test_dangerous_individual_perm_blocks():
    ex = MagicMock()
    ex.get_api_key_info.return_value = _safe_info(
        permissions={"Spot": ["SpotTrade", "Withdraw"]}
    )
    a = audit_api_key(ex)
    assert a.ok is False
    assert any("Withdraw" in w for w in a.warnings)


def test_readonly_key_blocks():
    ex = MagicMock()
    ex.get_api_key_info.return_value = _safe_info(readOnly=1)
    a = audit_api_key(ex)
    assert a.ok is False
    assert any("read-only" in w for w in a.warnings)


def test_no_trade_permission_blocks():
    ex = MagicMock()
    ex.get_api_key_info.return_value = _safe_info(
        permissions={"Spot": [], "Derivatives": [], "ContractTrade": []}
    )
    a = audit_api_key(ex)
    assert a.ok is False
    assert any("no trade permission" in w for w in a.warnings)


def test_missing_ip_whitelist_is_warning_not_blocker():
    ex = MagicMock()
    ex.get_api_key_info.return_value = _safe_info(ips=["*"])
    a = audit_api_key(ex)
    assert a.ok is True  # not a blocker
    assert a.has_ip_whitelist is False
    assert any("IP whitelist" in w for w in a.warnings)


def test_ip_whitelist_present_no_warning():
    ex = MagicMock()
    ex.get_api_key_info.return_value = _safe_info(ips=["1.2.3.4"])
    a = audit_api_key(ex)
    assert a.has_ip_whitelist is True
    assert not any("IP whitelist" in w for w in a.warnings)


def test_expired_key_blocks():
    ex = MagicMock()
    ex.get_api_key_info.return_value = _safe_info(expiredAt="2020-01-01T00:00:00Z")
    a = audit_api_key(ex)
    assert a.ok is False
    assert any("EXPIRED" in w for w in a.warnings)


def test_soon_expiring_key_warns_but_ok():
    from datetime import datetime, timedelta, timezone

    soon = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    ex = MagicMock()
    ex.get_api_key_info.return_value = _safe_info(expiredAt=soon)
    a = audit_api_key(ex)
    assert a.ok is True  # not expired yet
    assert any("expires in" in w for w in a.warnings)


def test_audit_failure_returns_error():
    ex = MagicMock()
    ex.get_api_key_info.side_effect = RuntimeError("api down")
    a = audit_api_key(ex)
    assert a.ok is False
    assert a.error is not None
    assert "api down" in a.error


def test_format_audit_renders_verdict():
    a = ApiKeyAudit(
        ok=True,
        note="test",
        permissions={"Spot": ["SpotTrade"]},
        ip_whitelist=["1.2.3.4"],
        expires_at=None,
        read_only=False,
    )
    out = format_audit(a)
    assert "SAFE" in out
    assert "test" in out
    a2 = ApiKeyAudit(
        ok=False,
        note="",
        permissions={},
        ip_whitelist=[],
        expires_at=None,
        read_only=True,
        warnings=["key is read-only"],
    )
    out2 = format_audit(a2)
    assert "ACTION NEEDED" in out2
    assert "read-only" in out2
