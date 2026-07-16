"""Multi-tenant Telegram bot for the carry-bot SaaS.

Each Telegram user = one platform user (BYOK). Commands:

  /start            — welcome + auto-register (7-day trial)
  /connect KEY SEC  — connect Bybit API key (audited, encrypted, message deleted)
  /status           — bot state, tier, subscription, positions
  /start_bot        — enable trading (requires connected key + active sub)
  /stop_bot         — pause trading (positions stay protected)
  /pricing          — subscription tiers
  /referral         — referral code + earnings
  /help             — command list

The bot runs the :class:`TenantRunner` supervisor as a background task so
per-user bots start/stop automatically as users connect keys and toggle.
"""

from __future__ import annotations

import asyncio
import os

import structlog
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from core.exchange import BybitExchange
from core.security import audit_api_key
from saas.database import DEFAULT_DB_PATH, Database
from saas.tenant_runner import TenantRunner
from saas.user_manager import UserManager

log = structlog.get_logger()

_PRICING_TEXT = (
    "*💎 Carry-Bot — тарифы*\n\n"
    "🆓 *Free* — 7 дней триал: 1 символ, до $50\n"
    "🥉 *Basic* — 500₽/мес: 3 символа, до $500\n"
    "🥈 *Pro* — 1500₽/мес: 10 символов, до $5000, ребалансировка\n"
    "🥇 *VIP* — 3000₽/мес: безлимит символов\n\n"
    "Оплата: ЮKassa (карта/СБП) или USDT.\n"
    "Напишите администратору для подключения тарифа."
)

_HELP_TEXT = (
    "*🤖 Carry-Bot — команды*\n\n"
    "🔗 `/connect KEY SECRET` — подключить API-ключ Bybit\n"
    "📊 `/status` — статус бота и позиции\n"
    "▶️ `/start_bot` — запустить торговлю\n"
    "⏸ `/stop_bot` — остановить торговлю\n"
    "💎 `/pricing` — тарифы\n"
    "🎁 `/referral` — реферальный код\n\n"
    "_⚠️ Создавайте API-ключ только с правами Spot+Derivatives Trade, "
    "БЕЗ права вывода. Бот проверит это автоматически._"
)


class TelegramSaaSBot:
    """Multi-tenant Telegram interface + tenant-runner supervisor."""

    def __init__(
        self,
        token: str,
        mgr: UserManager,
        runner: TenantRunner,
    ) -> None:
        self.mgr = mgr
        self.runner = runner
        self.app = (
            ApplicationBuilder().token(token).build()
        )
        self._register_handlers()

    def _register_handlers(self) -> None:
        add = self.app.add_handler
        add(CommandHandler("start", self._cmd_start))
        add(CommandHandler("help", self._cmd_help))
        add(CommandHandler("connect", self._cmd_connect))
        add(CommandHandler("status", self._cmd_status))
        add(CommandHandler("start_bot", self._cmd_start_bot))
        add(CommandHandler("stop_bot", self._cmd_stop_bot))
        add(CommandHandler("pricing", self._cmd_pricing))
        add(CommandHandler("referral", self._cmd_referral))

    # ------------------------------------------------------------- helpers

    def _get_or_register(self, update: Update):
        """Return the User for this Telegram chat, auto-registering on first /start."""
        tid = update.effective_user.id
        user = self.mgr.get_by_telegram_id(tid)
        return user

    @staticmethod
    async def _delete_message(update: Update) -> None:
        """Best-effort delete the user's message (hide API secrets from history)."""
        try:
            await update.message.delete()
        except Exception:  # noqa: BLE001 — deletion is best-effort
            pass

    # ------------------------------------------------------------- commands

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        tid = update.effective_user.id
        uname = update.effective_user.username or ""
        user = self.mgr.get_by_telegram_id(tid)
        if user is None:
            ref = ctx.args[0] if ctx.args else None
            user = self.mgr.register(tid, uname, referral_code=ref)
            await update.message.reply_text(
                f"👋 *Добро пожаловать!*\n\n"
                f"Вам активирован *7-дневный триал*.\n"
                f"Реферальный код: `{user.referral_code}`\n\n"
                f"Следующий шаг: `/connect` — подключите API-ключ Bybit.\n"
                f"Команды: `/help`",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await update.message.reply_text(
                f"👋 С возвращением, *{uname or 'трейдер'}*!\n"
                f"Тариф: *{user.effective_tier.value}*"
                f"{f' ({user.days_left():.0f} дн.)' if user.is_subscribed else ' (истёк)'}\n"
                f"Команды: `/help`",
                parse_mode=ParseMode.MARKDOWN,
            )

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(_HELP_TEXT, parse_mode=ParseMode.MARKDOWN)

    async def _cmd_pricing(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(_PRICING_TEXT, parse_mode=ParseMode.MARKDOWN)

    async def _cmd_connect(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Connect a Bybit API key: audit → encrypt → store. Deletes the message."""
        user = self._get_or_register(update)
        if user is None:
            await update.message.reply_text("Сначала нажмите /start для регистрации.")
            return
        if len(ctx.args) < 2:
            await update.message.reply_text(
                "Использование: `/connect API_KEY API_SECRET`\n\n"
                "⚠️ Создайте ключ с правами *только* Spot+Derivatives Trade, "
                "БЕЗ вывода средств.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        api_key, api_secret = ctx.args[0], ctx.args[1]
        await self._delete_message(update)  # hide secrets from chat history

        # Audit the key BEFORE storing it.
        await update.message.reply_text("🔍 Проверяю права ключа…")
        try:
            ex = BybitExchange(testnet=False, api_key=api_key, api_secret=api_secret)
            audit = await asyncio.to_thread(audit_api_key, ex)
        except Exception as exc:  # noqa: BLE001
            await update.message.reply_text(f"❌ Не удалось проверить ключ: {exc}")
            return

        if not audit.ok:
            reasons = "; ".join(audit.warnings) or "небезопасные права"
            await update.message.reply_text(
                f"❌ *Ключ отклонён*: {reasons}\n\n"
                "Создайте новый ключ БЕЗ прав Withdraw/Wallet.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        # Safe — encrypt and store.
        self.mgr.connect_api_key(user.id, api_key, api_secret)
        extra = ""
        if not audit.has_ip_whitelist:
            extra = ("\n\n⚠️ У ключа *нет IP-whitelist* — добавьте IP сервера в "
                     "настройках Bybit для дополнительной защиты.")
        await update.message.reply_text(
            "✅ *Ключ подключён и зашифрован!*\n"
            "Теперь запустите бота: `/start_bot`" + extra,
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._get_or_register(update)
        if user is None:
            await update.message.reply_text("Сначала /start")
            return
        sub = f"✅ активна ({user.days_left():.0f} дн.)" if user.is_subscribed else "❌ истекла"
        key = "✅ подключён" if user.has_api_key else "❌ не подключён"
        bot = "▶️ работает" if user.bot_enabled else "⏸ остановлен"
        # tenant last status (if running)
        tenants = {t["user_id"]: t for t in self.runner.tenant_status()}
        t = tenants.get(user.id)
        last = f"\nПоследний цикл: `{t['last_status']}`" if t else ""
        await update.message.reply_text(
            f"📊 *Статус*\n\n"
            f"Тариф: *{user.effective_tier.value}*\n"
            f"Подписка: {sub}\n"
            f"API-ключ: {key}\n"
            f"Бот: {bot}{last}",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _cmd_start_bot(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._get_or_register(update)
        if user is None:
            await update.message.reply_text("Сначала /start")
            return
        if not user.has_api_key:
            await update.message.reply_text("❌ Сначала подключите API-ключ: /connect")
            return
        if not user.is_subscribed:
            await update.message.reply_text(
                "❌ Подписка истекла. Обновите тариф: /pricing")
            return
        self.mgr.set_bot_enabled(user.id, True)
        await update.message.reply_text("▶️ *Бот запущен!* Позиции будут открываться автоматически.",
                                        parse_mode=ParseMode.MARKDOWN)

    async def _cmd_stop_bot(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._get_or_register(update)
        if user is None:
            await update.message.reply_text("Сначала /start")
            return
        self.mgr.set_bot_enabled(user.id, False)
        await update.message.reply_text(
            "⏸ *Бот остановлен.* Открытые позиции остаются под защитой "
            "(basis-guard, exchange-side SL).",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _cmd_referral(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._get_or_register(update)
        if user is None:
            await update.message.reply_text("Сначала /start")
            return
        summary = self.mgr.referral_summary(user.id)
        await update.message.reply_text(
            f"🎁 *Реферальная программа*\n\n"
            f"Ваш код: `{user.referral_code}`\n"
            f"Приглашено: *{summary['invited']}*\n"
            f"Заработано: *{summary['earned_usdt']:.2f} USDT*\n\n"
            f"Поделитесь ссылкой: `t.me/{ctx.bot.username}?start={user.referral_code}`",
            parse_mode=ParseMode.MARKDOWN,
        )

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Launch the tenant runner (background) + the Telegram poller."""
        runner_task = asyncio.create_task(self.runner.run())
        log.info("saas_bot_starting")
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        try:
            # Keep alive until interrupted.
            await asyncio.Event().wait()
        finally:
            self.runner.stop()
            runner_task.cancel()
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()


def build_from_env() -> TelegramSaaSBot:
    """Construct the full SaaS stack from environment variables."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    secret = os.environ["SAAS_MASTER_SECRET"]
    db_path = os.environ.get("SAAS_DB_PATH", DEFAULT_DB_PATH)
    db = Database(db_path)
    db.init()
    mgr = UserManager(db, secret)
    runner = TenantRunner(mgr)
    return TelegramSaaSBot(token, mgr, runner)
