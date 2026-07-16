"""User-facing Telegram bot for the multi-tenant SaaS platform.

This bot is the primary interface for *subscribers*.  It handles
registration, billing, subscription management, and referrals — the
"business" side of the platform.  It is deliberately separate from the
operator's admin bot (:mod:`bot.telegram_admin`) which controls the
trading engine directly.

Security model
--------------
* Any Telegram user can register (free 7-day trial, one account per TG id).
* Payment confirmation (``/admin_pay``) is restricted to configured admin IDs.
* The bot never stores or transmits API secrets in plaintext.

Commands
--------
    /start [ref_code]  - register (optional referral deep-link) / greeting
    /pricing           - show subscription plans & prices
    /subscribe <tier>  - create an invoice (basic / pro / vip)
    /pay               - payment instructions for your pending invoice
    /invoices          - list your recent invoices
    /myplan            - current tier, days left, limits
    /referral          - your referral code & earnings
    /admin_pay <id>    - [ADMIN] confirm a payment manually
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from saas.billing import BillingService
from saas.models import Tier, TierLimits
from saas.user_manager import UserManager
from utils.logger import get_logger

if TYPE_CHECKING:
    from saas.models import User

log = get_logger("saas.telegram")

#: Admin IDs allowed to confirm payments.  Injected at construction time.
_DEFAULT_ADMINS: list[int] = []


class SaaSTelegramBot:
    """User-facing Telegram bot for subscriptions, billing, and referrals."""

    def __init__(
        self,
        token: str,
        mgr: UserManager,
        billing: BillingService,
        admin_ids: list[int] | None = None,
    ) -> None:
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set for the SaaS bot.")
        self.mgr = mgr
        self.billing = billing
        self.admin_ids = admin_ids if admin_ids is not None else _DEFAULT_ADMINS

        self.app: Application = (
            ApplicationBuilder().token(token).build()
        )
        self._register_handlers()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _register_handlers(self) -> None:
        add = self.app.add_handler
        add(CommandHandler("start", self._cmd_start))
        add(CommandHandler("pricing", self._cmd_pricing))
        add(CommandHandler("subscribe", self._cmd_subscribe))
        add(CommandHandler("pay", self._cmd_pay))
        add(CommandHandler("invoices", self._cmd_invoices))
        add(CommandHandler("myplan", self._cmd_myplan))
        add(CommandHandler("referral", self._cmd_referral))
        add(CommandHandler("admin_pay", self._cmd_admin_pay))
        add(CommandHandler("help", self._cmd_help))

    async def start(self) -> None:
        """Start polling (blocks)."""
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()  # type: ignore[union-attr]
        log.info("saas_telegram.started")

    async def stop(self) -> None:
        """Graceful shutdown."""
        await self.app.updater.stop()  # type: ignore[union-attr]
        await self.app.stop()
        await self.app.shutdown()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _tg_user(self, update: Update) -> tuple[int, str] | None:
        """Return ``(telegram_id, username)`` or ``None`` if no sender."""
        u = update.effective_user
        if u is None:
            return None
        return u.id, (u.username or "")

    def _get_or_register(self, update: Update) -> User | None:
        """Return the user for this update, auto-registering on first contact.

        Returns ``None`` only if the Telegram identity is missing.
        """
        ident = self._tg_user(update)
        if ident is None:
            return None
        tg_id, username = ident
        user = self.mgr.get_by_telegram_id(tg_id)
        if user is not None:
            return user
        try:
            user = self.mgr.register(tg_id, username)
        except ValueError:
            # Race: registered by a concurrent request — fetch it.
            user = self.mgr.get_by_telegram_id(tg_id)
        return user

    def _is_admin(self, update: Update) -> bool:
        ident = self._tg_user(update)
        return bool(ident and ident[0] in self.admin_ids)

    @staticmethod
    def _fmt_price(v: float) -> str:
        """Format a USDT price without trailing zeros."""
        s = f"{v:.2f}"
        return s.rstrip("0").rstrip(".")

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------
    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Register (with optional referral) or greet a returning user."""
        user = self._get_or_register(update)
        if user is None:
            return

        # Parse optional referral code from deep-link payload: /start ref_XXXX
        ref_code = ""
        if ctx.args:
            ref_code = ctx.args[0].removeprefix("ref_")

        was_new = user.created_at > time.time() - 5  # registered just now
        if was_new and ref_code and user.referred_by is None:
            # Registration didn't capture the code (e.g. lazy path) — try linking.
            referrer = self.mgr.get_by_referral_code(ref_code.strip().upper())
            if referrer:
                with self.mgr.db.connect() as conn:
                    conn.execute(
                        "UPDATE users SET referred_by = ? WHERE id = ?",
                        (referrer.id, user.id),
                    )
                user = self.mgr.get_by_id(user.id)  # type: ignore[assignment]

        trial_days = user.days_left()
        text = (
            f"👋 Добро пожаловать в <b>AlgoTrader</b>!\n\n"
            f"Дельта-нейтральный carry-бот: зарабатывает на фандинге, "
            f"хеджируя риск движения цены.\n\n"
            f"🎯 Ваш тариф: <b>{user.effective_tier.value.upper()}</b>\n"
            f"⏳ Осталось: <b>{trial_days:.0f}</b> дней\n\n"
            f"Команды:\n"
            f"  /pricing — тарифы\n"
            f"  /subscribe pro — оформить подписку\n"
            f"  /myplan — мой тариф и лимиты\n"
            f"  /referral — реферальная программа\n"
            f"  /help — все команды"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _cmd_pricing(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Display the subscription pricing table."""
        rows = BillingService.pricing_table()
        lines = ["<b>💰 Тарифы AlgoTrader</b>\n"]
        for r in rows:
            rebal = "✅" if r["can_rebalance"] else "❌"
            lines.append(
                f"<b>{r['tier'].upper()}</b> — "
                f"{self._fmt_price(r['price_usdt'])} USDT / "
                f"{r['duration_days']:.0f} дней\n"
                f"   📊 Символов: {r['max_symbols']}\n"
                f"   💵 Макс. позиция: ${self._fmt_price(r['max_notional'])}\n"
                f"   🔄 Ребалансировка: {rebal}\n"
            )
        lines.append("🎁 FREE: 1 символ, $100, 7 дней триала")
        lines.append("\n/subscribe <b>pro</b> — оформить →")
        await update.message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.HTML,
        )

    async def _cmd_subscribe(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Create a pending invoice for the requested tier."""
        user = self._get_or_register(update)
        if user is None:
            return

        if not ctx.args:
            await update.message.reply_text(
                "Укажите тариф: <code>/subscribe basic</code> "
                "(или pro / vip)",
                parse_mode=ParseMode.HTML,
            )
            return

        raw = ctx.args[0].strip().lower()
        try:
            tier = Tier(raw)
        except ValueError:
            await update.message.reply_text(
                f"Неизвестный тариф «{raw}». Доступно: basic, pro, vip.",
            )
            return

        if tier == Tier.FREE:
            await update.message.reply_text(
                "FREE — бесплатный тариф, оплата не требуется. "
                "Используйте /subscribe basic (или pro / vip).",
            )
            return

        # Expire any stale pending invoices first.
        self.billing.expire_stale_invoices()

        invoice = self.billing.create_invoice(user.id, tier)  # type: ignore[arg-type]
        instructions = self.billing.gateway.payment_instructions(invoice)
        text = (
            f"🧾 Счёт <b>#{invoice.id}</b> создан\n"
            f"Тариф: <b>{tier.value.upper()}</b>\n"
            f"Сумма: <b>{self._fmt_price(invoice.amount_usdt)} USDT</b>\n\n"
            f"{_md_to_html(instructions)}\n\n"
            f"После оплаты дождитесь подтверждения или напишите админу."
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _cmd_pay(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Show payment instructions for the most recent pending invoice."""
        user = self._get_or_register(update)
        if user is None:
            return

        self.billing.expire_stale_invoices()
        invoice = self.billing.pending_invoice(user.id)  # type: ignore[arg-type]
        if invoice is None:
            await update.message.reply_text(
                "У вас нет ожидающих оплаты счетов.\n"
                "Создайте новый: /subscribe pro",
            )
            return

        instructions = self.billing.gateway.payment_instructions(invoice)
        text = (
            f"🧾 Счёт <b>#{invoice.id}</b> ({invoice.plan.upper()})\n"
            f"Сумма: <b>{self._fmt_price(invoice.amount_usdt)} USDT</b>\n\n"
            f"{_md_to_html(instructions)}"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _cmd_invoices(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """List the user's recent invoices."""
        user = self._get_or_register(update)
        if user is None:
            return

        invoices = self.billing.list_user_invoices(user.id)  # type: ignore[arg-type]
        if not invoices:
            await update.message.reply_text("У вас пока нет счетов.")
            return

        self.billing.expire_stale_invoices()
        lines = ["<b>🧾 Ваши счета</b>\n"]
        for inv in invoices[:10]:
            emoji = {
                "pending": "🟡", "paid": "✅",
                "expired": "⏰", "cancelled": "❌",
            }.get(inv.status.value, "❓")
            lines.append(
                f"{emoji} <b>#{inv.id}</b> {inv.plan.upper()} · "
                f"{self._fmt_price(inv.amount_usdt)} USDT · {inv.status.value}"
            )
        await update.message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.HTML,
        )

    async def _cmd_myplan(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Show the user's current tier, days left, and limits."""
        user = self._get_or_register(update)
        if user is None:
            return

        tier = user.effective_tier
        limits: TierLimits = user.limits
        days = user.days_left()
        key_status = "✅ подключён" if user.has_api_key else "❌ не подключён"

        text = (
            f"<b>📋 Мой тариф</b>\n\n"
            f"Тариф: <b>{tier.value.upper()}</b>\n"
            f"Осталось: <b>{days:.0f}</b> дней\n"
            f"API-ключ: {key_status}\n\n"
            f"<b>Лимиты:</b>\n"
            f"  📊 Символов: {limits.max_symbols}\n"
            f"  💵 Макс. позиция: ${self._fmt_price(limits.max_notional)}\n"
            f"  🔄 Ребалансировка: {'✅' if limits.can_rebalance else '❌'}\n"
            f"  📡 Расширенные алерты: {'✅' if limits.advanced_alerts else '❌'}\n"
        )
        if not user.is_subscribed and tier == Tier.FREE:
            text += "\n💡 /pricing — продлить или улучшить тариф"
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _cmd_referral(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Show the user's referral code and earnings."""
        user = self._get_or_register(update)
        if user is None:
            return

        summary: dict[str, Any] = self.mgr.referral_summary(user.id)  # type: ignore[arg-type]
        bot_username = (ctx.bot.username if ctx.bot else "algotrader_bot")
        ref_link = f"https://t.me/{bot_username}?start=ref_{user.referral_code}"

        text = (
            f"<b>🎁 Реферальная программа</b>\n\n"
            f"Ваш код: <code>{user.referral_code}</code>\n"
            f"Ваша ссылка:\n{ref_link}\n\n"
            f"👥 Приглашено: <b>{summary['invited']}</b>\n"
            f"💰 Заработано: <b>{summary['earned_usdt']:.2f}</b> USDT\n\n"
            f"Награда: 10% от каждой оплаты + 7 бонусных дней."
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _cmd_admin_pay(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """[ADMIN] Confirm a payment manually (ManualGateway flow)."""
        if not self._is_admin(update):
            await update.message.reply_text("⛔ Команда только для администратора.")
            return

        if not ctx.args:
            await update.message.reply_text(
                "Использование: <code>/admin_pay <invoice_id></code>",
                parse_mode=ParseMode.HTML,
            )
            return

        try:
            invoice_id = int(ctx.args[0])
        except ValueError:
            await update.message.reply_text("ID счёта должен быть числом.")
            return

        # Check current status *before* confirming (idempotency detection).
        existing = self.billing.get_invoice(invoice_id)
        if existing is None:
            await update.message.reply_text(f"Счёт #{invoice_id} не найден.")
            return

        if existing.status.value != "pending":
            await update.message.reply_text(
                f"ℹ️ Счёт #{existing.id} уже был обработан ранее "
                f"(статус: {existing.status.value}).",
            )
            return

        invoice = self.billing.confirm_payment(invoice_id, payment_id="manual_admin")
        target = self.mgr.get_by_id(invoice.user_id)
        name = f"@{target.username}" if target and target.username else f"id:{invoice.user_id}"
        await update.message.reply_text(
            f"✅ Счёт #{invoice.id} оплачен!\n"
            f"Пользователь: {name}\n"
            f"Тариф: {invoice.plan.upper()}\n"
            f"Сумма: {self._fmt_price(invoice.amount_usdt)} USDT",
        )

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """List all available commands."""
        self._get_or_register(update)  # lazy-register if needed
        is_admin = self._is_admin(update)
        lines = [
            "<b>📖 Команды AlgoTrader</b>\n",
            "/start — регистрация / приветствие",
            "/pricing — тарифы и цены",
            "/subscribe <i>basic|pro|vip</i> — создать счёт",
            "/pay — инструкция по оплате",
            "/invoices — мои счета",
            "/myplan — мой тариф и лимиты",
            "/referral — реферальная программа",
        ]
        if is_admin:
            lines.append("\n<b>Админ:</b>")
            lines.append("/admin_pay <i>id</i> — подтвердить оплату")
        await update.message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.HTML,
        )


# ═══════════════════════════════════════════════════════════════════════════
# Utilities
# ═══════════════════════════════════════════════════════════════════════════


def _md_to_html(text: str) -> str:
    """Convert simple Markdown backticks to HTML ``<code>`` tags.

    The payment gateways produce instructions with `` `backtick` `` code
    spans (Markdown).  This converts them to HTML so they render correctly
    when sent with ``ParseMode.HTML``.
    """
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", text)


# ═══════════════════════════════════════════════════════════════════════════
# CLI entry-point
# ═══════════════════════════════════════════════════════════════════════════


def main() -> None:
    """Run the SaaS Telegram bot standalone (for development / testing)."""
    import os

    from saas.database import Database

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    db_path = os.environ.get("SAAS_DB_PATH", "data/saas.db")
    master_secret = os.environ.get("SAAS_MASTER_SECRET", "dev-secret-change-me")
    admin_ids = [
        int(x) for x in os.environ.get("SAAS_ADMIN_IDS", "").split(",") if x.strip()
    ]
    usdt_wallet = os.environ.get("SAAS_USDT_WALLET", "")

    db = Database(db_path)
    db.init()
    mgr = UserManager(db, master_secret)

    from saas.billing import ManualGateway, UsdtGateway

    gateway = UsdtGateway(usdt_wallet) if usdt_wallet else ManualGateway()
    billing = BillingService(db, mgr, gateway)

    bot = SaaSTelegramBot(token, mgr, billing, admin_ids=admin_ids)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(bot.start())
        log.info("saas_telegram.polling — press Ctrl+C to stop")
        loop.run_forever()
    finally:
        loop.run_until_complete(bot.stop())
        loop.close()


if __name__ == "__main__":
    main()
