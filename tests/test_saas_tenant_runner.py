"""Tests for the multi-tenant bot orchestrator (TenantRunner)."""

from unittest.mock import MagicMock

import pytest

from saas import crypto
from saas.database import Database
from saas.models import Tier
from saas.tenant_runner import TenantRunner
from saas.user_manager import UserManager


def _mock_exchange():
    """A MagicMock exchange returning favorable carry conditions."""
    ex = MagicMock()
    ex.get_funding_rate.return_value = {
        "fundingRate": "0.0003", "markPrice": "65000", "lastPrice": "65000",
    }
    ex.get_spot_price.return_value = 65000.0
    ex.get_wallet_balance.return_value = {
        "list": [{"coin": [{"coin": "USDT", "walletBalance": "10000"}]}]
    }
    ex.get_positions.return_value = []
    ex.place_order.return_value = {"orderId": "p1"}
    ex.place_spot_order.return_value = {"orderId": "s1"}
    return ex


@pytest.fixture()
def runner(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.init()
    mgr = UserManager(db, crypto.generate_master_secret())
    exchanges = []

    def factory(api_key, api_secret):
        ex = _mock_exchange()
        exchanges.append(ex)
        return ex

    r = TenantRunner(mgr, exchange_factory=factory, poll_interval=0.01)
    r._exchanges = exchanges  # expose for assertions
    return r


def _make_user(runner, tid, tier=Tier.BASIC, days=30):
    u = runner.mgr.register(tid, f"user{tid}")
    if tier != Tier.FREE or days > 7:
        runner.mgr.extend_subscription(u.id, tier, days=days, amount=500)
    runner.mgr.connect_api_key(u.id, "key", "secret")
    runner.mgr.set_bot_enabled(u.id, True)
    return runner.mgr.get_by_id(u.id)


# ----------------------------------------------------------------- single poll


def test_poll_user_opens_position(runner):
    u = _make_user(runner, 1001)
    status = runner.poll_user(u.id)
    assert "open" in status
    # tenant was created
    assert u.id in runner._tenants


def test_poll_user_caches_tenant(runner):
    u = _make_user(runner, 1002)
    runner.poll_user(u.id)
    first = runner._tenants[u.id]
    runner.poll_user(u.id)
    assert runner._tenants[u.id] is first  # same tenant reused


def test_poll_user_no_api_key(runner):
    u = runner.mgr.register(1003, "nokeys")
    runner.mgr.set_bot_enabled(u.id, True)
    # no api key connected
    status = runner.poll_user(u.id)
    assert "no api" in status


# ----------------------------------------------------------------- tier limits


def test_free_tier_caps_notional(runner):
    """A FREE-tier user's config must cap max_notional at 50."""
    u = _make_user(runner, 1004, tier=Tier.FREE, days=0)
    # expire the trial so effective_tier = FREE
    import time
    runner.mgr.db  # noqa: B018
    with runner.mgr.db.connect() as conn:
        conn.execute("UPDATE users SET subscription_until=? WHERE id=?",
                     (time.time() - 1, u.id))
    bc = runner.mgr.get_bot_config(u.id)
    cfg = runner.build_carry_config(runner.mgr.get_by_id(u.id), bc)
    assert cfg.max_notional == 50.0


def test_pro_tier_allows_higher_notional(runner):
    u = _make_user(runner, 1005, tier=Tier.PRO)
    bc = runner.mgr.get_bot_config(u.id)
    bc.max_notional = 3000.0
    runner.mgr.set_bot_config(bc)
    cfg = runner.build_carry_config(runner.mgr.get_by_id(u.id), bc)
    assert cfg.max_notional == 3000.0  # under PRO cap of 5000


def test_explicit_notional_capped_at_tier(runner):
    """Even if user sets max_notional above tier limit, it's clamped."""
    u = _make_user(runner, 1006, tier=Tier.BASIC)  # cap 500
    bc = runner.mgr.get_bot_config(u.id)
    bc.max_notional = 99999.0
    runner.mgr.set_bot_config(bc)
    cfg = runner.build_carry_config(runner.mgr.get_by_id(u.id), bc)
    assert cfg.max_notional == 500.0


# ----------------------------------------------------------------- subscription


def test_expired_subscription_monitoring_mode(runner):
    """Expired sub → can_open=False → no new opens (monitoring only)."""
    import time
    u = _make_user(runner, 1007, tier=Tier.PRO)
    # expire
    with runner.mgr.db.connect() as conn:
        conn.execute("UPDATE users SET subscription_until=? WHERE id=?",
                     (time.time() - 1, u.id))
    status = runner.poll_user(u.id)
    # funding is favorable but can_open=False → "none" (no open)
    assert "none" in status or "cooldown" in status
    # verify no order was placed
    ex = runner._tenants[u.id].exchange
    ex.place_order.assert_not_called()


# ----------------------------------------------------------------- sync


def test_sync_tenants_adds_new(runner):
    u = _make_user(runner, 1008)
    runner._sync_tenants()
    assert u.id in runner._tenants


def test_sync_tenants_removes_disabled(runner):
    u = _make_user(runner, 1009)
    runner._sync_tenants()
    assert u.id in runner._tenants
    # disable the bot
    runner.mgr.set_bot_enabled(u.id, False)
    runner._sync_tenants()
    assert u.id not in runner._tenants


def test_sync_tenants_ignores_no_keys(runner):
    """A user with bot_enabled but no API keys is not in list_active_bots."""
    u = runner.mgr.register(1010, "enabled_no_keys")
    runner.mgr.set_bot_enabled(u.id, True)
    runner._sync_tenants()
    assert u.id not in runner._tenants


# ----------------------------------------------------------------- status


def test_tenant_status_snapshot(runner):
    u = _make_user(runner, 1011)
    runner.poll_user(u.id)
    snap = runner.tenant_status()
    assert len(snap) == 1
    assert snap[0]["user_id"] == u.id
    assert "last_status" in snap[0]
