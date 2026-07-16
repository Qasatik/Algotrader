"""Repository & business logic for SaaS users, subscriptions, and configs.

Bridges :mod:`saas.database` (raw rows) ↔ :mod:`saas.models` (dataclasses) and
encrypts/decrypts API keys via :mod:`saas.crypto`. All write operations go
through the ``Database`` context manager (auto-commit / rollback).
"""

from __future__ import annotations

import secrets
import string
import time
from typing import Any

from saas import crypto
from saas.database import Database
from saas.models import (
    TRIAL_DURATION_S,
    BotConfig,
    Tier,
    User,
)

_REFERRAL_ALPHABET = string.ascii_uppercase + string.digits  # no ambiguous chars
_REFERRAL_LENGTH = 8

#: A sqlite3.Row (or any mapping supporting ``row["col"]``).
_Row = Any


def _generate_referral_code() -> str:
    return "".join(secrets.choice(_REFERRAL_ALPHABET) for _ in range(_REFERRAL_LENGTH))


def _row_to_user(row: _Row) -> User:
    return User(
        id=row["id"],
        telegram_id=row["telegram_id"],
        username=row["username"],
        tier=Tier(row["tier"]),
        subscription_until=row["subscription_until"],
        referral_code=row["referral_code"],
        referred_by=row["referred_by"],
        api_key_encrypted=row["api_key_encrypted"],
        api_secret_encrypted=row["api_secret_encrypted"],
        bot_enabled=bool(row["bot_enabled"]),
        created_at=row["created_at"],
    )


def _row_to_config(row: _Row) -> BotConfig:
    return BotConfig(
        user_id=row["user_id"],
        top_n=row["top_n"],
        equity_fraction=row["equity_fraction"],
        max_notional=row["max_notional"],
        min_funding=row["min_funding"],
        leverage=row["leverage"],
        stop_loss_pct=row["stop_loss_pct"],
        scan_symbols=row["scan_symbols"],
    )


class UserManager:
    """CRUD + subscription/referral logic over the SaaS database."""

    def __init__(self, db: Database, master_secret: str) -> None:
        self.db = db
        self.master_secret = master_secret

    # ------------------------------------------------------------------ users

    def register(
        self, telegram_id: int, username: str = "", referral_code: str | None = None,
    ) -> User:
        """Register a new user with a free trial.

        If *referral_code* matches an existing user, link the referrer.
        Returns the created user. Raises ``ValueError`` if the telegram_id
        is already registered (use :meth:`get_by_telegram_id` first).
        """
        referrer = None
        if referral_code:
            referrer = self.get_by_referral_code(referral_code.strip().upper())

        now = time.time()
        # Ensure a unique referral code (retry on the rare collision).
        for _ in range(10):
            code = _generate_referral_code()
            if not self.get_by_referral_code(code):
                break

        with self.db.connect() as conn:
            cur = conn.execute(
                """INSERT INTO users
                   (telegram_id, username, tier, subscription_until,
                    referral_code, referred_by, created_at)
                   VALUES (?, ?, 'free', ?, ?, ?, ?)""",
                (telegram_id, username, now + TRIAL_DURATION_S,
                 code, referrer.id if referrer else None, now),
            )
            user_id = cur.lastrowid
        user = self.get_by_id(user_id)  # type: ignore[arg-type]
        assert user is not None
        return user

    def get_by_telegram_id(self, telegram_id: int) -> User | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        return _row_to_user(row) if row else None

    def get_by_id(self, user_id: int) -> User | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return _row_to_user(row) if row else None

    def get_by_referral_code(self, code: str) -> User | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE referral_code = ?", (code,)
            ).fetchone()
        return _row_to_user(row) if row else None

    # ------------------------------------------------------------- API keys

    def connect_api_key(self, user_id: int, api_key: str, api_secret: str) -> None:
        """Encrypt and store the user's Bybit API credentials."""
        key_tok = crypto.encrypt(api_key, self.master_secret)
        sec_tok = crypto.encrypt(api_secret, self.master_secret)
        with self.db.connect() as conn:
            conn.execute(
                """UPDATE users SET api_key_encrypted = ?, api_secret_encrypted = ?
                   WHERE id = ?""",
                (key_tok, sec_tok, user_id),
            )

    def get_api_credentials(self, user_id: int) -> tuple[str, str] | None:
        """Decrypt and return ``(api_key, api_secret)`` or None if not set."""
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT api_key_encrypted, api_secret_encrypted FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if not row or not row["api_key_encrypted"] or not row["api_secret_encrypted"]:
            return None
        return (
            crypto.decrypt(row["api_key_encrypted"], self.master_secret),
            crypto.decrypt(row["api_secret_encrypted"], self.master_secret),
        )

    # -------------------------------------------------------- subscriptions

    def set_subscription(
        self, user_id: int, tier: Tier | str, paid_until: float, amount: float,
        payment_method: str = "", payment_id: str = "",
    ) -> None:
        """Record a payment and extend the user's subscription.

        If the current subscription hasn't expired yet, *paid_until* should be
        relative to NOW (the caller computes the extension); this method just
        stores the absolute timestamp.
        """
        t = Tier(tier) if not isinstance(tier, Tier) else tier
        now = time.time()
        with self.db.connect() as conn:
            conn.execute(
                """UPDATE users SET tier = ?, subscription_until = ? WHERE id = ?""",
                (t.value, paid_until, user_id),
            )
            conn.execute(
                """INSERT INTO subscriptions
                   (user_id, plan, amount, paid_until, payment_method, payment_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, t.value, amount, paid_until, payment_method, payment_id, now),
            )

    def extend_subscription(
        self, user_id: int, tier: Tier | str, days: float, amount: float,
        payment_method: str = "", payment_id: str = "",
    ) -> float:
        """Extend the subscription by *days* (from now or current expiry).

        Returns the new absolute ``paid_until`` timestamp.
        """
        user = self.get_by_id(user_id)
        base = max(user.subscription_until, time.time()) if user else time.time()
        new_until = base + days * 86400.0
        self.set_subscription(user_id, tier, new_until, amount, payment_method, payment_id)
        return new_until

    # ------------------------------------------------------------ bot config

    def get_bot_config(self, user_id: int) -> BotConfig:
        """Return the user's config, creating a default row if absent."""
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM bot_configs WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO bot_configs (user_id) VALUES (?)""", (user_id,)
                )
                row = conn.execute(
                    "SELECT * FROM bot_configs WHERE user_id = ?", (user_id,)
                ).fetchone()
        return _row_to_config(row)

    def set_bot_config(self, config: BotConfig) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO bot_configs
                   (user_id, top_n, equity_fraction, max_notional, min_funding,
                    leverage, stop_loss_pct, scan_symbols)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     top_n=excluded.top_n, equity_fraction=excluded.equity_fraction,
                     max_notional=excluded.max_notional, min_funding=excluded.min_funding,
                     leverage=excluded.leverage, stop_loss_pct=excluded.stop_loss_pct,
                     scan_symbols=excluded.scan_symbols""",
                (config.user_id, config.top_n, config.equity_fraction,
                 config.max_notional, config.min_funding, config.leverage,
                 config.stop_loss_pct, config.scan_symbols),
            )

    def set_bot_enabled(self, user_id: int, enabled: bool) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE users SET bot_enabled = ? WHERE id = ?",
                (1 if enabled else 0, user_id),
            )

    def list_active_bots(self) -> list[User]:
        """Users whose bot is enabled AND has API keys connected."""
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM users
                   WHERE bot_enabled = 1
                     AND api_key_encrypted IS NOT NULL
                     AND api_secret_encrypted IS NOT NULL"""
            ).fetchall()
        return [_row_to_user(r) for r in rows]

    # ------------------------------------------------------------- referrals

    def add_referral_earning(
        self, referrer_id: int, referred_id: int, source: str, amount_usdt: float,
    ) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO referral_earnings
                   (referrer_id, referred_id, source, amount_usdt, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (referrer_id, referred_id, source, amount_usdt, time.time()),
            )

    def referral_summary(self, user_id: int) -> dict[str, Any]:
        """Aggregate referral stats: count invited + total earned."""
        with self.db.connect() as conn:
            invited = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE referred_by = ?", (user_id,)
            ).fetchone()["n"]
            earned = conn.execute(
                "SELECT COALESCE(SUM(amount_usdt), 0) AS s FROM referral_earnings "
                "WHERE referrer_id = ?", (user_id,)
            ).fetchone()["s"]
        return {"invited": invited, "earned_usdt": float(earned)}
