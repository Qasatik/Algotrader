"""Tests for the SaaS user manager (CRUD, subscriptions, referrals, API keys)."""

import time

import pytest

from saas import crypto
from saas.database import Database
from saas.models import BotConfig, Tier
from saas.user_manager import UserManager


@pytest.fixture()
def mgr(tmp_path):
    db = Database(str(tmp_path / "test_saas.db"))
    db.init()
    secret = crypto.generate_master_secret()
    return UserManager(db, secret)


# ----------------------------------------------------------------- registration


def test_register_creates_user_with_trial(mgr):
    u = mgr.register(telegram_id=111, username="alice")
    assert u.id is not None
    assert u.telegram_id == 111
    assert u.username == "alice"
    assert u.tier == Tier.FREE
    assert u.is_subscribed  # trial active
    assert u.days_left() <= 7
    assert len(u.referral_code) == 8


def test_register_with_referral_links_referrer(mgr):
    referrer = mgr.register(telegram_id=1, username="boss")
    invited = mgr.register(telegram_id=2, username="newbie",
                           referral_code=referrer.referral_code)
    assert invited.referred_by == referrer.id
    summary = mgr.referral_summary(referrer.id)
    assert summary["invited"] == 1


def test_register_invalid_referral_code_ignored(mgr):
    u = mgr.register(telegram_id=5, username="x", referral_code="DOESNOTEXIST")
    assert u.referred_by is None


def test_referral_code_is_unique(mgr):
    codes = {mgr.register(i, f"u{i}").referral_code for i in range(30)}
    assert len(codes) == 30


# ----------------------------------------------------------------- lookups


def test_get_by_telegram_id(mgr):
    mgr.register(222, "bob")
    found = mgr.get_by_telegram_id(222)
    assert found is not None
    assert found.username == "bob"
    assert mgr.get_by_telegram_id(999999) is None


# ----------------------------------------------------------------- API keys


def test_connect_and_retrieve_api_key(mgr):
    u = mgr.register(333, "carol")
    mgr.connect_api_key(u.id, "MY_API_KEY", "MY_API_SECRET")
    creds = mgr.get_api_credentials(u.id)
    assert creds == ("MY_API_KEY", "MY_API_SECRET")


def test_api_key_encrypted_at_rest(mgr):
    """The raw DB must NOT contain the plaintext key."""
    u = mgr.register(334, "dave")
    mgr.connect_api_key(u.id, "PLAINTEXT_SECRET_123", "PLAINTEXT_SECRET_456")
    with mgr.db.connect() as conn:
        row = conn.execute("SELECT api_key_encrypted FROM users WHERE id = ?",
                           (u.id,)).fetchone()
    assert "PLAINTEXT_SECRET_123" not in row["api_key_encrypted"]


def test_get_api_credentials_none_when_not_set(mgr):
    u = mgr.register(335, "eve")
    assert mgr.get_api_credentials(u.id) is None


# ----------------------------------------------------------------- subscriptions


def test_extend_subscription(mgr):
    u = mgr.register(444, "frank")
    # User starts with a 7-day trial; extending +30 stacks → ~37 days total.
    new_until = mgr.extend_subscription(u.id, Tier.PRO, days=30, amount=1500,
                                        payment_method="yookassa")
    refreshed = mgr.get_by_id(u.id)
    assert refreshed.tier == Tier.PRO
    assert refreshed.subscription_until == new_until
    assert refreshed.is_subscribed
    assert 36 <= refreshed.days_left() <= 37


def test_extend_stacks_on_existing(mgr):
    """Extending again adds from the current expiry, not from now."""
    u = mgr.register(445, "grace")
    first = mgr.extend_subscription(u.id, Tier.BASIC, days=30, amount=500)
    # extend immediately — should add 30 days to `first`, not to now
    second = mgr.extend_subscription(u.id, Tier.BASIC, days=30, amount=500)
    assert second == pytest.approx(first + 30 * 86400, rel=1e-6)


def test_expired_subscription_downgrades_to_free_limits(mgr):
    u = mgr.register(446, "heidi")
    # Manually expire the subscription.
    with mgr.db.connect() as conn:
        conn.execute("UPDATE users SET subscription_until = ? WHERE id = ?",
                     (time.time() - 1, u.id))
    refreshed = mgr.get_by_id(u.id)
    assert not refreshed.is_subscribed
    assert refreshed.effective_tier == Tier.FREE
    assert refreshed.limits.max_notional == 100.0


# ----------------------------------------------------------------- bot config


def test_bot_config_default_created(mgr):
    u = mgr.register(555, "ivan")
    cfg = mgr.get_bot_config(u.id)
    assert cfg.user_id == u.id
    assert cfg.top_n == 1
    assert cfg.equity_fraction == 0.5


def test_bot_config_update(mgr):
    u = mgr.register(556, "judy")
    mgr.set_bot_config(BotConfig(user_id=u.id, top_n=3, equity_fraction=0.7,
                                 max_notional=300.0, min_funding=0.00005))
    cfg = mgr.get_bot_config(u.id)
    assert cfg.top_n == 3
    assert cfg.equity_fraction == 0.7
    assert cfg.max_notional == 300.0


def test_resolved_max_notional_caps_at_tier(mgr):
    """An explicit max_notional can never exceed the tier limit."""
    u = mgr.register(557, "karl")
    mgr.extend_subscription(u.id, Tier.BASIC, days=30, amount=500)
    cfg = BotConfig(user_id=u.id, max_notional=99999.0)  # way above BASIC $500
    # BASIC tier limit is 500
    assert cfg.resolved_max_notional(Tier.BASIC) == 500.0


# ----------------------------------------------------------------- bot lifecycle


def test_list_active_bots(mgr):
    a = mgr.register(666, "a")
    b = mgr.register(667, "b")
    c = mgr.register(668, "c")
    # only 'a' has keys + enabled
    mgr.connect_api_key(a.id, "k", "s")
    mgr.set_bot_enabled(a.id, True)
    # 'b' has keys but bot disabled
    mgr.connect_api_key(b.id, "k", "s")
    # 'c' has bot enabled but no keys
    mgr.set_bot_enabled(c.id, True)
    active = mgr.list_active_bots()
    assert len(active) == 1
    assert active[0].id == a.id


# ----------------------------------------------------------------- referrals


def test_referral_earnings(mgr):
    boss = mgr.register(1, "boss")
    newbie = mgr.register(2, "newbie", referral_code=boss.referral_code)
    mgr.add_referral_earning(boss.id, newbie.id, "bybit", 5.50)
    mgr.add_referral_earning(boss.id, newbie.id, "internal", 1.00)
    summary = mgr.referral_summary(boss.id)
    assert summary["invited"] == 1
    assert summary["earned_usdt"] == pytest.approx(6.50)
