"""Tests for the SaaS billing system (invoices, payments, referral bonuses)."""

from __future__ import annotations

import time

import pytest

from saas.billing import (
    INVOICE_TTL_S,
    PLAN_DURATION_DAYS,
    PLAN_PRICES,
    REFERRAL_COMMISSION,
    BillingService,
    InvoiceStatus,
    ManualGateway,
    UsdtGateway,
)
from saas.crypto import generate_master_secret
from saas.database import Database
from saas.models import Tier
from saas.user_manager import UserManager

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def billing(tmp_path) -> BillingService:
    """Fresh billing service with a temp-file SQLite DB."""
    db = Database(str(tmp_path / "test.db"))
    db.init()
    secret = generate_master_secret()
    mgr = UserManager(db, secret)
    return BillingService(db, mgr, ManualGateway())


@pytest.fixture()
def billing_usdt(tmp_path) -> tuple[BillingService, UserManager]:
    """Billing service with USDT gateway."""
    db = Database(str(tmp_path / "test.db"))
    db.init()
    secret = generate_master_secret()
    mgr = UserManager(db, secret)
    gw = UsdtGateway("TXYZ1234567890ABCDEF")
    return BillingService(db, mgr, gw), mgr


def _make_user(mgr: UserManager, tid: int = 100):
    return mgr.register(tid, f"user{tid}")


# ── Invoice creation ──────────────────────────────────────────────────


class TestCreateInvoice:
    def test_creates_pending_invoice_with_correct_price(self, billing):
        u = _make_user(billing.mgr)
        inv = billing.create_invoice(u.id, Tier.PRO)
        assert inv.status == InvoiceStatus.PENDING
        assert inv.amount_usdt == PLAN_PRICES[Tier.PRO]
        assert inv.plan == "pro"
        assert inv.payment_method == "manual"

    def test_invoice_has_expiry(self, billing):
        u = _make_user(billing.mgr)
        inv = billing.create_invoice(u.id, Tier.BASIC)
        assert inv.expires_at > inv.created_at
        assert inv.expires_at - inv.created_at == pytest.approx(INVOICE_TTL_S, abs=2)

    def test_free_tier_raises(self, billing):
        u = _make_user(billing.mgr)
        with pytest.raises(ValueError, match="Cannot create invoice"):
            billing.create_invoice(u.id, Tier.FREE)

    def test_usdt_gateway_sets_method(self, billing_usdt):
        billing, mgr = billing_usdt
        u = _make_user(mgr)
        inv = billing.create_invoice(u.id, Tier.VIP)
        assert inv.payment_method == "usdt"


# ── Payment confirmation ──────────────────────────────────────────────


class TestConfirmPayment:
    def test_confirms_and_extends_subscription(self, billing):
        u = _make_user(billing.mgr)
        inv = billing.create_invoice(u.id, Tier.PRO)
        result = billing.confirm_payment(inv.id, payment_id="tx_001")

        assert result is not None
        assert result.status == InvoiceStatus.PAID
        assert result.paid_at is not None
        assert result.payment_id == "tx_001"

        # Subscription extended + tier upgraded
        updated = billing.mgr.get_by_id(u.id)
        assert updated.tier == Tier.PRO
        assert updated.is_subscribed
        days = updated.days_left()
        # 7-day trial + 30-day payment = ~37 days
        assert 36 <= days <= 37

    def test_idempotent_double_confirm(self, billing):
        u = _make_user(billing.mgr)
        inv = billing.create_invoice(u.id, Tier.BASIC)

        first = billing.confirm_payment(inv.id, "tx_A")
        second = billing.confirm_payment(inv.id, "tx_B")

        assert first.status == InvoiceStatus.PAID
        assert second.status == InvoiceStatus.PAID
        # Second call should NOT overwrite the payment_id
        assert second.payment_id == "tx_A"

    def test_confirm_nonexistent_returns_none(self, billing):
        assert billing.confirm_payment(99999) is None

    def test_confirm_expired_invoice_is_noop(self, billing):
        u = _make_user(billing.mgr)
        inv = billing.create_invoice(u.id, Tier.BASIC)
        # Manually expire it
        with billing.db.connect() as conn:
            conn.execute(
                "UPDATE invoices SET status='expired' WHERE id=?", (inv.id,)
            )
        result = billing.confirm_payment(inv.id)
        assert result is not None
        assert result.status == InvoiceStatus.EXPIRED
        # Subscription should NOT have been extended
        user = billing.mgr.get_by_id(u.id)
        assert user.tier == Tier.FREE


# ── Referral bonus ────────────────────────────────────────────────────


class TestReferralBonus:
    def test_referrer_gets_commission(self, billing):
        referrer = billing.mgr.register(111, "referrer")
        user = billing.mgr.register(112, "invited", referral_code=referrer.referral_code)

        inv = billing.create_invoice(user.id, Tier.PRO)
        billing.confirm_payment(inv.id, "tx_ref")

        summary = billing.mgr.referral_summary(referrer.id)
        expected = PLAN_PRICES[Tier.PRO] * REFERRAL_COMMISSION
        assert summary["earned_usdt"] == pytest.approx(expected, rel=0.01)
        assert summary["invited"] == 1

    def test_referrer_gets_bonus_days(self, billing):
        referrer = billing.mgr.register(111, "referrer")
        # Give referrer an active PRO subscription
        billing.mgr.extend_subscription(referrer.id, Tier.PRO, 10, 16.0)
        user = billing.mgr.register(112, "invited", referral_code=referrer.referral_code)

        days_before = billing.mgr.get_by_id(referrer.id).days_left()
        inv = billing.create_invoice(user.id, Tier.BASIC)
        billing.confirm_payment(inv.id, "tx_bonus")

        days_after = billing.mgr.get_by_id(referrer.id).days_left()
        # Should have gained ~7 bonus days
        gained = days_after - days_before
        assert 6.5 <= gained <= 7.5

    def test_no_referral_bonus_without_referrer(self, billing):
        user = billing.mgr.register(111, "lonely")
        inv = billing.create_invoice(user.id, Tier.BASIC)
        billing.confirm_payment(inv.id, "tx_noref")
        # No error, just no referral earnings
        assert billing.mgr.referral_summary(user.id)["earned_usdt"] == 0


# ── Invoice expiry ────────────────────────────────────────────────────


class TestInvoiceExpiry:
    def test_expire_stale_invoices(self, billing):
        u = _make_user(billing.mgr)
        inv = billing.create_invoice(u.id, Tier.BASIC)
        # Simulate time passing — set expiry to past
        with billing.db.connect() as conn:
            conn.execute(
                "UPDATE invoices SET expires_at=? WHERE id=?",
                (time.time() - 1, inv.id),
            )
        count = billing.expire_stale_invoices()
        assert count == 1
        assert billing.get_invoice(inv.id).status == InvoiceStatus.EXPIRED

    def test_expire_only_pending(self, billing):
        u = _make_user(billing.mgr)
        inv = billing.create_invoice(u.id, Tier.BASIC)
        billing.confirm_payment(inv.id, "tx_paid")
        # Set expiry to past but status is 'paid'
        with billing.db.connect() as conn:
            conn.execute(
                "UPDATE invoices SET expires_at=? WHERE id=?",
                (time.time() - 1, inv.id),
            )
        count = billing.expire_stale_invoices()
        assert count == 0  # paid invoices are not expired


# ── Invoice queries ───────────────────────────────────────────────────


class TestInvoiceQueries:
    def test_list_user_invoices(self, billing):
        u = _make_user(billing.mgr)
        billing.create_invoice(u.id, Tier.BASIC)
        billing.create_invoice(u.id, Tier.PRO)
        invs = billing.list_user_invoices(u.id)
        assert len(invs) == 2
        # Most recent first
        assert invs[0].created_at >= invs[1].created_at

    def test_pending_invoice(self, billing):
        u = _make_user(billing.mgr)
        inv = billing.create_invoice(u.id, Tier.BASIC)
        assert billing.pending_invoice(u.id).id == inv.id

    def test_pending_invoice_none_after_payment(self, billing):
        u = _make_user(billing.mgr)
        inv = billing.create_invoice(u.id, Tier.BASIC)
        billing.confirm_payment(inv.id, "tx")
        assert billing.pending_invoice(u.id) is None


# ── Gateway instructions ──────────────────────────────────────────────


class TestGateways:
    def test_manual_gateway_instructions(self, billing):
        u = _make_user(billing.mgr)
        inv = billing.create_invoice(u.id, Tier.PRO)
        gw = ManualGateway()
        text = gw.payment_instructions(inv)
        assert "8.00" in text  # PRO = $8/mo after price reduction
        assert str(inv.id) in text

    def test_usdt_gateway_instructions(self, billing_usdt):
        billing, mgr = billing_usdt
        u = _make_user(mgr)
        inv = billing.create_invoice(u.id, Tier.PRO)
        gw = billing.gateway
        data = gw.create_invoice(inv)
        assert data["wallet"] == "TXYZ1234567890ABCDEF"
        assert data["network"] == "TRC-20 (Tron)"
        assert "memo" in data
        text = gw.payment_instructions(inv)
        assert "TXYZ1234567890ABCDEF" in text
        assert "TRC-20" in text

    def test_usdt_memo_is_unique(self, billing_usdt):
        billing, mgr = billing_usdt
        u = _make_user(mgr)
        inv1 = billing.create_invoice(u.id, Tier.BASIC)
        inv2 = billing.create_invoice(u.id, Tier.BASIC)
        m1 = billing.gateway.create_invoice(inv1)["memo"]
        m2 = billing.gateway.create_invoice(inv2)["memo"]
        assert m1 != m2


# ── Pricing table ─────────────────────────────────────────────────────


class TestPricingTable:
    def test_pricing_table_has_all_paid_tiers(self):
        table = BillingService.pricing_table()
        tiers = [row["tier"] for row in table]
        assert tiers == ["basic", "pro", "vip"]
        for row in table:
            assert row["price_usdt"] > 0
            assert row["duration_days"] == PLAN_DURATION_DAYS
