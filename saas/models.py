"""Domain models for the multi-tenant SaaS layer.

Pure dataclasses + tier-limit definitions — no I/O, no side effects. The
database layer (:mod:`saas.database`) maps rows ↔ these objects.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class Tier(str, Enum):
    """Subscription tiers, ordered by capability."""

    FREE = "free"    # trial / expired — 1 symbol, tiny notional
    BASIC = "basic"  # 3 symbols, $500 notional
    PRO = "pro"      # 10 symbols, $5000 notional
    VIP = "vip"      # unlimited


@dataclass(frozen=True)
class TierLimits:
    """Hard caps enforced per tier (symbols, notional, features)."""

    max_symbols: int
    max_notional: float
    can_rebalance: bool
    advanced_alerts: bool

    @staticmethod
    def for_tier(tier: Tier | str) -> TierLimits:
        t = Tier(tier) if not isinstance(tier, Tier) else tier
        return _TIER_LIMITS[t]


_TIER_LIMITS: dict[Tier, TierLimits] = {
    Tier.FREE: TierLimits(max_symbols=1, max_notional=100.0,
                          can_rebalance=False, advanced_alerts=False),
    Tier.BASIC: TierLimits(max_symbols=3, max_notional=500.0,
                           can_rebalance=False, advanced_alerts=True),
    Tier.PRO: TierLimits(max_symbols=10, max_notional=5000.0,
                         can_rebalance=True, advanced_alerts=True),
    Tier.VIP: TierLimits(max_symbols=999, max_notional=999_999.0,
                         can_rebalance=True, advanced_alerts=True),
}

#: How long a free trial lasts (seconds).
TRIAL_DURATION_S = 7 * 24 * 3600


@dataclass
class User:
    """A registered platform user (one Telegram account = one user)."""

    id: int | None
    telegram_id: int
    username: str
    tier: Tier = Tier.FREE
    subscription_until: float = 0.0  # unix ts; 0 = no active subscription
    referral_code: str = ""
    referred_by: int | None = None  # user.id of the referrer
    # Encrypted Bybit API credentials (AES-256-GCM tokens; None = not connected).
    api_key_encrypted: str | None = None
    api_secret_encrypted: str | None = None
    bot_enabled: bool = False
    created_at: float = field(default_factory=time.time)

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key_encrypted and self.api_secret_encrypted)

    @property
    def is_subscribed(self) -> bool:
        """True if the subscription is still valid (not expired)."""
        return self.subscription_until > time.time()

    @property
    def effective_tier(self) -> Tier:
        """The tier the user effectively operates under right now.

        An expired subscription downgrades to FREE (trial-baseline limits)
        so the bot stops opening new positions but data is preserved.
        """
        return self.tier if self.is_subscribed else Tier.FREE

    @property
    def limits(self) -> TierLimits:
        return TierLimits.for_tier(self.effective_tier)

    def days_left(self) -> float:
        """Whole days remaining on the subscription (0 if expired)."""
        if not self.is_subscribed:
            return 0.0
        return (self.subscription_until - time.time()) / 86400.0


@dataclass
class BotConfig:
    """Per-user trading configuration (stored separately from the user row)."""

    user_id: int
    top_n: int = 1
    equity_fraction: float = 0.5
    max_notional: float | None = None  # None = use tier limit
    min_funding: float = 0.0001
    leverage: int = 2
    stop_loss_pct: float = 15.0
    # Comma-separated candidate symbols for rotation (empty = default universe).
    scan_symbols: str = ""

    def resolved_max_notional(self, tier: Tier | str) -> float:
        """The notional cap actually applied: explicit override or tier limit."""
        if self.max_notional is not None:
            return min(self.max_notional, TierLimits.for_tier(tier).max_notional)
        return TierLimits.for_tier(tier).max_notional


@dataclass
class Subscription:
    """A payment record (one row per billing event)."""

    id: int | None
    user_id: int
    plan: str          # tier name purchased
    amount: float      # rubles paid
    paid_until: float  # unix ts the subscription extends to
    payment_method: str = ""  # "yookassa" / "usdt" / "manual"
    payment_id: str = ""      # gateway transaction id
    created_at: float = field(default_factory=time.time)


@dataclass
class ReferralEarning:
    """A single referral commission event."""

    id: int | None
    referrer_id: int
    referred_id: int
    source: str        # "internal" / "bybit"
    amount_usdt: float
    created_at: float = field(default_factory=time.time)
