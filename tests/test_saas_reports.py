"""Tests for the SaaS reporting & retention-alert service (saas/reports.py)."""
from __future__ import annotations

import time

import pytest

from saas.billing import BillingService, ManualGateway
from saas.database import Database
from saas.models import Tier
from saas.reports import ReportService
from saas.user_manager import UserManager

_MASTER = "test-master-secret-for-reports"


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def svc(tmp_path):
    """A ReportService wired to a temp DB."""
    db = Database(str(tmp_path / "saas.db"))
    db.init()
    mgr = UserManager(db, _MASTER)
    return ReportService(db, mgr)


@pytest.fixture()
def billing(tmp_path):
    """A BillingService for creating paid subscriptions."""
    db = Database(str(tmp_path / "saas.db"))
    db.init()
    mgr = UserManager(db, _MASTER)
    return BillingService(db, mgr, ManualGateway())


def _make_user(svc, tg_id, username="user"):
    """Register a user and return it."""
    return svc.mgr.register(tg_id, username)


def _pay_for(svc, billing, user, tier, days=30):
    """Create + confirm an invoice, extending the subscription."""
    inv = billing.create_invoice(user.id, tier)
    billing.confirm_payment(inv.id, "tx_test")
    return svc.mgr.get_by_id(user.id)


# ═══════════════════════════════════════════════════════════════════════════
# Queries
# ═══════════════════════════════════════════════════════════════════════════


class TestSubscribedUsers:
    def test_returns_only_subscribed(self, svc, billing):
        u1 = _make_user(svc, 101, "alice")
        u2 = _make_user(svc, 102, "bob")
        _pay_for(svc, billing, u2, Tier.PRO)

        subscribed = svc.subscribed_users()
        ids = [u.id for u in subscribed]
        # u1 is on trial (subscribed_until > now), u2 paid.
        assert u2.id in ids
        # Trial users also count as subscribed.
        assert u1.id in ids

    def test_excludes_expired(self, svc, billing):
        u = _make_user(svc, 101, "alice")
        # Manually expire the subscription.
        with svc.db.connect() as conn:
            conn.execute(
                "UPDATE users SET subscription_until = ? WHERE id = ?",
                (time.time() - 100, u.id),
            )
        subscribed = svc.subscribed_users()
        assert u.id not in [s.id for s in subscribed]


class TestExpiringUsers:
    def test_finds_expiring_within_window(self, svc, billing):
        u = _make_user(svc, 101, "alice")
        _pay_for(svc, billing, u, Tier.PRO)
        # Set subscription to expire in 2 days.
        with svc.db.connect() as conn:
            conn.execute(
                "UPDATE users SET subscription_until = ? WHERE id = ?",
                (time.time() + 2 * 86400, u.id),
            )
        expiring = svc.expiring_users(within_days=3)
        assert u.id in [e.id for e in expiring]

    def test_excludes_far_future(self, svc, billing):
        u = _make_user(svc, 101, "alice")
        _pay_for(svc, billing, u, Tier.PRO)
        # 30 days left — not within 3-day window.
        expiring = svc.expiring_users(within_days=3)
        assert u.id not in [e.id for e in expiring]

    def test_excludes_already_expired(self, svc):
        u = _make_user(svc, 101, "alice")
        with svc.db.connect() as conn:
            conn.execute(
                "UPDATE users SET subscription_until = ? WHERE id = ?",
                (time.time() - 100, u.id),
            )
        expiring = svc.expiring_users(within_days=3)
        assert u.id not in [e.id for e in expiring]


class TestExpiredUsers:
    def test_finds_recently_lapsed(self, svc, billing):
        u = _make_user(svc, 101, "alice")
        _pay_for(svc, billing, u, Tier.PRO)
        # Set subscription to have expired 1 hour ago.
        with svc.db.connect() as conn:
            conn.execute(
                "UPDATE users SET subscription_until = ? WHERE id = ?",
                (time.time() - 3600, u.id),
            )
        expired = svc.expired_users(since_hours=25)
        assert u.id in [e.id for e in expired]

    def test_excludes_old_lapse(self, svc, billing):
        u = _make_user(svc, 101, "alice")
        _pay_for(svc, billing, u, Tier.PRO)
        # Expired 48 hours ago — outside the 25-hour window.
        with svc.db.connect() as conn:
            conn.execute(
                "UPDATE users SET subscription_until = ? WHERE id = ?",
                (time.time() - 48 * 3600, u.id),
            )
        expired = svc.expired_users(since_hours=25)
        assert u.id not in [e.id for e in expired]

    def test_excludes_free_tier(self, svc):
        """FREE-tier users never get 'expired' notices."""
        u = _make_user(svc, 101, "alice")
        with svc.db.connect() as conn:
            conn.execute(
                "UPDATE users SET subscription_until = ? WHERE id = ?",
                (time.time() - 3600, u.id),
            )
        expired = svc.expired_users(since_hours=25)
        assert u.id not in [e.id for e in expired]


# ═══════════════════════════════════════════════════════════════════════════
# Formatters
# ═══════════════════════════════════════════════════════════════════════════


class TestFormatDailyReport:
    def test_includes_tier_and_days(self, svc):
        u = _make_user(svc, 101, "alice")
        msg = svc.format_daily_report(u)
        assert "FREE" in msg
        assert "7" in msg  # trial = 7 days

    def test_includes_equity_when_provided(self, svc):
        u = _make_user(svc, 101, "alice")
        msg = svc.format_daily_report(u, equity_usdt=1234.56)
        assert "1,234.56" in msg

    def test_includes_bot_status(self, svc):
        u = _make_user(svc, 101, "alice")
        msg = svc.format_daily_report(u, bot_status="hold: funding ok")
        assert "hold: funding ok" in msg

    def test_warns_when_expiring(self, svc):
        u = _make_user(svc, 101, "alice")
        with svc.db.connect() as conn:
            conn.execute(
                "UPDATE users SET subscription_until = ? WHERE id = ?",
                (time.time() + 2 * 86400, u.id),
            )
        u = svc.mgr.get_by_id(u.id)
        msg = svc.format_daily_report(u)
        assert "скоро истечёт" in msg


class TestFormatExpiryWarning:
    def test_includes_tier_and_renewal(self, svc, billing):
        u = _make_user(svc, 101, "alice")
        _pay_for(svc, billing, u, Tier.PRO)
        with svc.db.connect() as conn:
            conn.execute(
                "UPDATE users SET subscription_until = ? WHERE id = ?",
                (time.time() + 1 * 86400, u.id),
            )
        u = svc.mgr.get_by_id(u.id)
        msg = svc.format_expiry_warning(u)
        assert "PRO" in msg
        assert "/subscribe" in msg
        assert "/referral" in msg

    def test_correct_day_word(self, svc):
        u = _make_user(svc, 101, "alice")
        # 1 day left → "день" (singular)
        with svc.db.connect() as conn:
            conn.execute(
                "UPDATE users SET subscription_until = ? WHERE id = ?",
                (time.time() + 1 * 86400, u.id),
            )
        u = svc.mgr.get_by_id(u.id)
        msg = svc.format_expiry_warning(u)
        assert "день" in msg


class TestFormatExpiredNotice:
    def test_includes_tier_and_renewal(self, svc, billing):
        u = _make_user(svc, 101, "alice")
        u = _pay_for(svc, billing, u, Tier.BASIC)
        msg = svc.format_expired_notice(u)
        assert "BASIC" in msg
        assert "истекла" in msg
        assert "/subscribe" in msg
        assert "мониторинга" in msg  # mentions monitoring mode


class TestFormatSubscriptionConfirmation:
    def test_includes_tier_and_days(self, svc):
        msg = svc.format_subscription_confirmation("pro", 30)
        assert "PRO" in msg
        assert "30" in msg
        assert "активирована" in msg


# ═══════════════════════════════════════════════════════════════════════════
# Batch helpers
# ═══════════════════════════════════════════════════════════════════════════


class TestExpiryWarningsToSend:
    def test_returns_pairs(self, svc, billing):
        u = _make_user(svc, 101, "alice")
        _pay_for(svc, billing, u, Tier.PRO)
        with svc.db.connect() as conn:
            conn.execute(
                "UPDATE users SET subscription_until = ? WHERE id = ?",
                (time.time() + 2 * 86400, u.id),
            )
        pairs = svc.expiry_warnings_to_send()
        assert len(pairs) == 1
        user, msg = pairs[0]
        assert user.id == u.id
        assert "истекает" in msg

    def test_dedup_at_multiple_thresholds(self, svc, billing):
        """A user at 1 day should appear once (not once per threshold)."""
        u = _make_user(svc, 101, "alice")
        _pay_for(svc, billing, u, Tier.PRO)
        with svc.db.connect() as conn:
            conn.execute(
                "UPDATE users SET subscription_until = ? WHERE id = ?",
                (time.time() + 0.5 * 86400, u.id),  # < 1 day
            )
        pairs = svc.expiry_warnings_to_send()
        user_ids = [p[0].id for p in pairs]
        assert user_ids.count(u.id) == 1

    def test_empty_when_none_expiring(self, svc, billing):
        u = _make_user(svc, 101, "alice")
        _pay_for(svc, billing, u, Tier.PRO)
        # 30 days left — no warnings.
        assert svc.expiry_warnings_to_send() == []


class TestExpiredNoticesToSend:
    def test_returns_pairs(self, svc, billing):
        u = _make_user(svc, 101, "alice")
        _pay_for(svc, billing, u, Tier.VIP)
        with svc.db.connect() as conn:
            conn.execute(
                "UPDATE users SET subscription_until = ? WHERE id = ?",
                (time.time() - 3600, u.id),
            )
        pairs = svc.expired_notices_to_send()
        assert len(pairs) == 1
        assert "истекла" in pairs[0][1]


class TestDailyReportsToSend:
    def test_returns_all_subscribed(self, svc, billing):
        u1 = _make_user(svc, 101, "alice")
        u2 = _make_user(svc, 102, "bob")
        _pay_for(svc, billing, u2, Tier.PRO)

        pairs = svc.daily_reports_to_send()
        ids = [p[0].id for p in pairs]
        assert u1.id in ids  # trial user
        assert u2.id in ids  # paid user

    def test_includes_bot_status(self, svc):
        u = _make_user(svc, 101, "alice")
        pairs = svc.daily_reports_to_send(statuses={u.id: "open: funding 0.02%"})
        msg = pairs[0][1]
        assert "open: funding 0.02%" in msg

    def test_empty_statuses_handled(self, svc):
        _make_user(svc, 101, "alice")
        pairs = svc.daily_reports_to_send()
        # Should still work without statuses.
        assert len(pairs) == 1
