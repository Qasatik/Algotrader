"""Reporting & retention-alert service for the SaaS platform.

Generates daily P&L summaries and subscription-expiry warnings.  The
service is **pure** (no Telegram / network side-effects) so it can be
unit-tested in isolation.  A background task wires the formatted
messages to the :class:`~saas.telegram_saas.SaaSTelegramBot` for delivery.

Retention philosophy
--------------------
Notifications are the #1 churn-reduction lever.  A user who sees the
bot working (daily report) and is reminded before their subscription
lapses (expiry warning) is far less likely to churn.

Key methods
-----------
    * :meth:`subscribed_users` — all users with active subscriptions.
    * :meth:`expiring_users` — users whose sub expires within *N* days.
    * :meth:`format_daily_report` — HTML daily summary for one user.
    * :meth:`format_expiry_warning` — HTML "renew soon" message.
    * :meth:`format_expired_notice` — HTML "subscription expired" message.
"""
from __future__ import annotations

import time

from saas.database import Database
from saas.models import User
from saas.user_manager import UserManager, _row_to_user

#: Send expiry warnings at these day thresholds (3 days and 1 day before).
EXPIRY_WARNING_DAYS: list[int] = [3, 1]

#: Hours between daily reports (24 = once per day).
DAILY_REPORT_INTERVAL_H = 24


class ReportService:
    """Pure reporting logic — formats messages, never sends them."""

    def __init__(self, db: Database, mgr: UserManager) -> None:
        self.db = db
        self.mgr = mgr

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def subscribed_users(self) -> list[User]:
        """All users with an active (non-expired) subscription."""
        now = time.time()
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM users WHERE subscription_until > ? "
                "ORDER BY subscription_until ASC",
                (now,),
            ).fetchall()
        return [_row_to_user(r) for r in rows]

    def expiring_users(self, within_days: float = 3.0) -> list[User]:
        """Users whose subscription expires within *within_days* days.

        Only includes users who are still subscribed (not yet expired).
        """
        now = time.time()
        cutoff = now + within_days * 86400
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM users "
                "WHERE subscription_until > ? AND subscription_until <= ? "
                "ORDER BY subscription_until ASC",
                (now, cutoff),
            ).fetchall()
        return [_row_to_user(r) for r in rows]

    def expired_users(self, since_hours: float = 24.0) -> list[User]:
        """Users whose subscription expired within the last *since_hours*.

        Identifies recently-lapsed users (tier ≠ FREE but subscription
        expired) so we can send a "your subscription expired" notice.
        """
        now = time.time()
        cutoff = now - since_hours * 3600
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM users "
                "WHERE tier != 'free' AND subscription_until <= ? "
                "AND subscription_until >= ? "
                "ORDER BY subscription_until DESC",
                (now, cutoff),
            ).fetchall()
        return [_row_to_user(r) for r in rows]

    def all_users_with_bots(self) -> list[User]:
        """Users with bot enabled AND API keys connected."""
        return self.mgr.list_active_bots()

    # ------------------------------------------------------------------
    # Formatters (pure functions → HTML strings)
    # ------------------------------------------------------------------
    def format_daily_report(
        self,
        user: User,
        bot_status: str = "",
        equity_usdt: float | None = None,
    ) -> str:
        """Format a daily summary message for a user.

        Parameters
        ----------
        user
            The user to report on.
        bot_status
            Last status string from the TenantRunner (e.g. "hold: funding ok").
        equity_usdt
            Current account equity in USDT, if known.
        """
        tier = user.effective_tier
        days = user.days_left()
        lines = [
            "📊 <b>Ежедневный отчёт AlgoTrader</b>\n",
            f"Тариф: <b>{tier.value.upper()}</b>",
            f"⏳ Осталось: <b>{days:.0f}</b> дн.",
        ]
        if equity_usdt is not None:
            lines.append(f"💰 Эквити: <b>${equity_usdt:,.2f}</b>")
        if bot_status:
            lines.append(f"🤖 Бот: {bot_status}")
        if user.has_api_key:
            lines.append("🔑 API-ключ: подключён")
        else:
            lines.append("⚠️ API-ключ не подключён — /connect")
        lines.append("\n📋 /myplan — детали тарифа")
        if days <= 3:
            lines.append("⚠️ Подписка скоро истечёт — /pricing")
        return "\n".join(lines)

    def format_expiry_warning(self, user: User) -> str:
        """Format a subscription-expiry warning (sent 3/1 days before)."""
        days = user.days_left()
        days_int = round(days)
        day_word = "день" if days_int == 1 else "дня" if days_int < 5 else "дней"
        return (
            f"⏰ <b>Подписка истекает!</b>\n\n"
            f"Тариф <b>{user.tier.value.upper()}</b> истекает через "
            f"<b>{days:.0f}</b> {day_word}.\n\n"
            f"Чтобы бот продолжил торговать:\n"
            f"  /subscribe {user.tier.value}\n\n"
            f"🎁 Бесплатные дни за друзей: /referral"
        )

    def format_expired_notice(self, user: User) -> str:
        """Format a 'subscription expired' notice (sent after lapse)."""
        return (
            f"❌ <b>Подписка истекла</b>\n\n"
            f"Тариф <b>{user.tier.value.upper()}</b> больше не активен.\n"
            f"Бот переведён в режим мониторинга (новые позиции не открываются).\n\n"
            f"Продлить:\n"
            f"  /subscribe {user.tier.value}\n\n"
            f"🎁 Бесплатные дни за друзей: /referral"
        )

    def format_subscription_confirmation(
        self, tier_name: str, days: float,
    ) -> str:
        """Format a 'payment confirmed' notification."""
        return (
            f"✅ <b>Подписка активирована!</b>\n\n"
            f"Тариф: <b>{tier_name.upper()}</b>\n"
            f"Действует: <b>{days:.0f}</b> дней\n\n"
            f"🚀 Бот готов к работе. Не забудьте подключить API-ключ: /connect"
        )

    # ------------------------------------------------------------------
    # Batch helpers (for background tasks)
    # ------------------------------------------------------------------
    def expiry_warnings_to_send(self) -> list[tuple[User, str]]:
        """Return ``(user, message)`` pairs for users needing expiry warnings.

        Checks both the 3-day and 1-day thresholds.  Each user appears at
        most once (the most urgent threshold wins).
        """
        result: list[tuple[User, str]] = []
        for threshold in sorted(EXPIRY_WARNING_DAYS):
            users = self.expiring_users(within_days=float(threshold))
            already = {u.id for u, _ in result}
            for u in users:
                if u.id not in already:
                    result.append((u, self.format_expiry_warning(u)))
                    already.add(u.id)
        return result

    def expired_notices_to_send(self) -> list[tuple[User, str]]:
        """Return ``(user, message)`` pairs for recently-lapsed users."""
        users = self.expired_users(since_hours=25)
        return [(u, self.format_expired_notice(u)) for u in users]

    def daily_reports_to_send(
        self,
        statuses: dict[int, str] | None = None,
    ) -> list[tuple[User, str]]:
        """Return ``(user, message)`` pairs for daily reports.

        Parameters
        ----------
        statuses
            Optional mapping of ``user_id → bot_status`` (from
            :meth:`~saas.tenant_runner.TenantRunner.tenant_status`).
        """
        statuses = statuses or {}
        result: list[tuple[User, str]] = []
        for u in self.subscribed_users():
            status = statuses.get(u.id, "")  # type: ignore[arg-type]
            msg = self.format_daily_report(u, bot_status=status)
            result.append((u, msg))
        return result
