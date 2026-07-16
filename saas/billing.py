"""Billing & payment system for the SaaS platform.

Provides a payment-gateway abstraction (Strategy pattern) so the platform can
accept payments via multiple channels (USDT, ЮKassa, manual admin confirm)
without coupling the business logic to any single provider.

Key components:
    * :data:`PLAN_PRICES` — monthly price per tier (USDT).
    * :class:`Invoice` / :class:`InvoiceStatus` — billing records.
    * :class:`PaymentGateway` — abstract gateway (create / check / confirm).
    * :class:`ManualGateway` — admin-confirmed payments (MVP / testing).
    * :class:`UsdtGateway` — generates USDT TRC-20 payment instructions.
    * :class:`BillingService` — orchestrates invoices, payments, referral bonuses.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from saas.database import Database
from saas.models import Tier
from saas.user_manager import UserManager

#: Monthly subscription price per tier (USDT).
#: Approximate conversions of 500 / 1500 / 3000 ₽ at ~85 ₽/$.
PLAN_PRICES: dict[Tier, float] = {
    Tier.BASIC: 6.0,
    Tier.PRO: 16.0,
    Tier.VIP: 30.0,
}

#: How many days a subscription purchase grants.
PLAN_DURATION_DAYS = 30.0

#: Invoice validity window (seconds). Unpaid invoices expire after this.
INVOICE_TTL_S = 24 * 3600

#: Referral commission rate (fraction of payment → referrer credit).
REFERRAL_COMMISSION = 0.10

#: Bonus days the referrer receives when a referral pays.
REFERRAL_BONUS_DAYS = 7.0


class InvoiceStatus(str, Enum):
    """Lifecycle states for a payment invoice."""

    PENDING = "pending"
    PAID = "paid"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


@dataclass
class Invoice:
    """A single billing invoice (one payment attempt)."""

    id: int | None
    user_id: int
    plan: str          # tier name being purchased
    amount_usdt: float
    status: InvoiceStatus = InvoiceStatus.PENDING
    payment_method: str = ""   # "usdt" / "manual" / "yookassa"
    payment_id: str = ""       # gateway transaction reference
    created_at: float = 0.0
    expires_at: float = 0.0
    paid_at: float | None = None

    @property
    def is_expired(self) -> bool:
        """True if the invoice is past its TTL and still pending."""
        return self.status == InvoiceStatus.PENDING and self.expires_at < time.time()


def _row_to_invoice(row: Any) -> Invoice:
    """Map a ``sqlite3.Row`` to an :class:`Invoice`."""
    return Invoice(
        id=row["id"],
        user_id=row["user_id"],
        plan=row["plan"],
        amount_usdt=row["amount_usdt"],
        status=InvoiceStatus(row["status"]),
        payment_method=row["payment_method"],
        payment_id=row["payment_id"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        paid_at=row["paid_at"],
    )


# ═══════════════════════════════════════════════════════════════════════════
# Payment Gateway abstraction (Strategy pattern)
# ═══════════════════════════════════════════════════════════════════════════


class PaymentGateway:
    """Abstract base for payment providers.

    Concrete gateways implement :meth:`create_invoice` (generate payment
    instructions) and :meth:`payment_instructions` (human-readable text for
    the user).  Payment *confirmation* is centralised in
    :class:`BillingService.confirm_payment` so the flow is identical
    regardless of gateway.
    """

    method: str = "abstract"

    def create_invoice(self, invoice: Invoice) -> dict[str, Any]:
        """Generate gateway-specific data for an invoice (e.g. payment address).

        Returns a dict with at least ``"instructions"`` (str) for the user.
        """
        raise NotImplementedError

    def payment_instructions(self, invoice: Invoice) -> str:
        """Human-readable payment instructions for the user."""
        raise NotImplementedError


class ManualGateway(PaymentGateway):
    """Admin-confirmed payments — simplest gateway for MVP / testing.

    No external API. The admin manually marks invoices as paid after
    receiving payment out-of-band (bank transfer, hand-shake, etc.).
    """

    method = "manual"

    def create_invoice(self, invoice: Invoice) -> dict[str, Any]:
        return {
            "instructions": (
                f"Перевод {invoice.amount_usdt:.2f} USDT на кошелёк администратора. "
                f"После оплаты сообщите админу номер счёта #{invoice.id}."
            ),
        }

    def payment_instructions(self, invoice: Invoice) -> str:
        return self.create_invoice(invoice)["instructions"]


class UsdtGateway(PaymentGateway):
    """USDT TRC-20 crypto payments — zero commission, no chargebacks.

    Generates a unique payment reference (memo) per invoice so incoming
    blockchain transfers can be matched to the right user. Actual on-chain
    verification is done by a separate scanner / webhook (future); for MVP
    the admin confirms manually after checking the blockchain.
    """

    method = "usdt"

    def __init__(self, wallet_address: str) -> None:
        self.wallet_address = wallet_address

    def create_invoice(self, invoice: Invoice) -> dict[str, Any]:
        memo = secrets.token_hex(4).upper()
        return {
            "wallet": self.wallet_address,
            "network": "TRC-20 (Tron)",
            "amount": f"{invoice.amount_usdt:.2f} USDT",
            "memo": memo,
            "instructions": (
                f"Отправьте {invoice.amount_usdt:.2f} USDT (сеть TRC-20) на адрес:\n"
                f"  `{self.wallet_address}`\n"
                f"Memo/комментарий: `{memo}`\n"
                f"Счёт #{invoice.id} действителен 24 часа."
            ),
        }

    def payment_instructions(self, invoice: Invoice) -> str:
        return self.create_invoice(invoice)["instructions"]


# ═══════════════════════════════════════════════════════════════════════════
# Billing Service — orchestrates invoices, payments, referral bonuses
# ═══════════════════════════════════════════════════════════════════════════


class BillingService:
    """High-level billing operations wrapping :class:`UserManager` + gateway.

    Usage::

        billing = BillingService(db, user_manager, UsdtGateway("T..."))
        inv = billing.create_invoice(user_id, Tier.PRO)
        # ... user pays ...
        billing.confirm_payment(inv.id, payment_id="tx123")
    """

    def __init__(
        self,
        db: Database,
        mgr: UserManager,
        gateway: PaymentGateway | None = None,
    ) -> None:
        self.db = db
        self.mgr = mgr
        self.gateway = gateway or ManualGateway()

    # ── Invoice CRUD ──────────────────────────────────────────────────

    def create_invoice(self, user_id: int, tier: Tier | str) -> Invoice:
        """Create a pending invoice for *tier* (30-day subscription).

        Raises :class:`ValueError` if the tier is FREE or unknown.
        """
        t = Tier(tier) if not isinstance(tier, Tier) else tier
        if t == Tier.FREE or t not in PLAN_PRICES:
            raise ValueError(f"Cannot create invoice for tier '{t}'")

        amount = PLAN_PRICES[t]
        now = time.time()
        with self.db.connect() as conn:
            cur = conn.execute(
                """INSERT INTO invoices
                   (user_id, plan, amount_usdt, status, payment_method,
                    created_at, expires_at)
                   VALUES (?, ?, ?, 'pending', ?, ?, ?)""",
                (user_id, t.value, amount, self.gateway.method,
                 now, now + INVOICE_TTL_S),
            )
            invoice_id = cur.lastrowid
        invoice = self.get_invoice(invoice_id)  # type: ignore[arg-type]
        assert invoice is not None
        return invoice

    def get_invoice(self, invoice_id: int) -> Invoice | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM invoices WHERE id = ?", (invoice_id,)
            ).fetchone()
        return _row_to_invoice(row) if row else None

    def list_user_invoices(self, user_id: int, limit: int = 20) -> list[Invoice]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM invoices WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [_row_to_invoice(r) for r in rows]

    def pending_invoice(self, user_id: int) -> Invoice | None:
        """Return the most recent pending invoice for a user, or None."""
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM invoices WHERE user_id = ? AND status = 'pending' "
                "ORDER BY created_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        return _row_to_invoice(row) if row else None

    # ── Payment confirmation ──────────────────────────────────────────

    def confirm_payment(
        self, invoice_id: int, payment_id: str = "",
    ) -> Invoice | None:
        """Mark an invoice as paid and activate the subscription.

        This is the **single source of truth** for payment confirmation —
        regardless of which gateway detected the payment. Idempotent: a
        second call on an already-paid invoice is a no-op.

        Side effects:
            * Extends the user's subscription by 30 days.
            * Upgrades the user's tier.
            * If the user was referred, grants the referrer a commission
              (10% credit + 7 bonus days).

        Returns the updated invoice, or None if not found / already paid.
        """
        invoice = self.get_invoice(invoice_id)
        if invoice is None:
            return None
        if invoice.status != InvoiceStatus.PENDING:
            return invoice  # idempotent — already processed

        now = time.time()

        # 1. Mark invoice paid
        with self.db.connect() as conn:
            conn.execute(
                """UPDATE invoices SET status = 'paid', payment_id = ?, paid_at = ?
                   WHERE id = ?""",
                (payment_id, now, invoice_id),
            )

        # 2. Extend subscription + upgrade tier
        self.mgr.extend_subscription(
            invoice.user_id, invoice.plan, PLAN_DURATION_DAYS,
            invoice.amount_usdt, invoice.payment_method, payment_id,
        )

        # 3. Referral bonus
        self._process_referral_bonus(invoice)

        return self.get_invoice(invoice_id)

    def _process_referral_bonus(self, invoice: Invoice) -> None:
        """Grant commission + bonus days to the referrer if applicable."""
        user = self.mgr.get_by_id(invoice.user_id)
        if not user or not user.referred_by:
            return

        referrer_id = user.referred_by
        commission = invoice.amount_usdt * REFERRAL_COMMISSION

        # Record the commission
        self.mgr.add_referral_earning(
            referrer_id, invoice.user_id, "internal", commission,
        )

        # Grant bonus days to the referrer's subscription
        referrer = self.mgr.get_by_id(referrer_id)
        if referrer and referrer.is_subscribed:
            # Extend the referrer's current subscription
            new_until = max(referrer.subscription_until, time.time()) + REFERRAL_BONUS_DAYS * 86400
            self.mgr.set_subscription(
                referrer_id, referrer.tier, new_until, 0.0,
                "referral_bonus", f"ref_from_{invoice.user_id}",
            )

    # ── Expiry management ─────────────────────────────────────────────

    def expire_stale_invoices(self) -> int:
        """Mark all pending invoices past their TTL as expired.

        Returns the count of newly-expired invoices. Call periodically
        (e.g. every hour) from a background task.
        """
        now = time.time()
        with self.db.connect() as conn:
            cur = conn.execute(
                "UPDATE invoices SET status = 'expired' "
                "WHERE status = 'pending' AND expires_at < ?",
                (now,),
            )
            return cur.rowcount

    # ── Pricing info ──────────────────────────────────────────────────

    @staticmethod
    def pricing_table() -> list[dict[str, Any]]:
        """Return a list of tier pricing info for display (TG / landing)."""
        from saas.models import TierLimits

        result = []
        for tier in [Tier.BASIC, Tier.PRO, Tier.VIP]:
            limits = TierLimits.for_tier(tier)
            result.append({
                "tier": tier.value,
                "price_usdt": PLAN_PRICES[tier],
                "duration_days": PLAN_DURATION_DAYS,
                "max_symbols": limits.max_symbols,
                "max_notional": limits.max_notional,
                "can_rebalance": limits.can_rebalance,
            })
        return result
