"""Telegram admin bot for the Bybit algo trading engine.

Security model (defense in depth):
  1. Telegram user-ID whitelist (TELEGRAM_ADMIN_IDS).
  2. TOTP 2FA unlock: privileged commands require a valid 6-digit code.
     A successful unlock creates a short-lived session (SESSION_TTL seconds).
  3. Read-only commands (/status, /help) are available without 2FA so an
     operator can always inspect state; anything that moves money requires
     an active 2FA session.

Commands:
  /start        - greeting + instructions
  /help         - list commands
  /setup_2fa    - generate a QR code to bind an authenticator app
  /auth <code>  - unlock privileged commands with a TOTP code
  /status       - engine state, equity, buffer, stats (read-only)
  /positions    - open positions (read-only)
  /pnl          - realized + unrealized PnL (read-only)
  /start_engine - start trading  [2FA]
  /stop_engine  - graceful stop  [2FA]
  /pause        - pause order placement  [2FA]
  /resume       - resume order placement  [2FA]
  /kill         - EMERGENCY: stop + flatten  [2FA]
"""
from __future__ import annotations

import io
import time
from dataclasses import dataclass, field

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from bot.alerts import set_alert_sender
from config.settings import get_settings
from core.engine import TradingEngine
from security.totp import generate_secret, provisioning_uri, verify
from utils.logger import get_logger

log = get_logger("telegram")


@dataclass
class _Session:
    unlocked_until: float = 0.0
    failed_attempts: int = 0


@dataclass
class _SessionStore:
    """Per-user 2FA sessions, keyed by Telegram user id."""
    sessions: dict[int, _Session] = field(default_factory=dict)

    def is_unlocked(self, uid: int) -> bool:
        s = self.sessions.get(uid)
        return bool(s and time.time() < s.unlocked_until)

    def unlock(self, uid: int, ttl: int) -> None:
        self.sessions[uid] = _Session(unlocked_until=time.time() + ttl)

    def lock(self, uid: int) -> None:
        self.sessions.pop(uid, None)

    def record_failure(self, uid: int) -> int:
        s = self.sessions.setdefault(uid, _Session())
        s.failed_attempts += 1
        return s.failed_attempts


class TelegramAdminBot:
    """Builds and runs the Telegram admin application."""

    def __init__(self, engine: TradingEngine) -> None:
        self.settings = get_settings()
        self.engine = engine
        self.sessions = _SessionStore()
        self._log = log

        if not self.settings.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")

        self.app: Application = (
            ApplicationBuilder()
            .token(self.settings.telegram_bot_token)
            .build()
        )
        self._register_handlers()

    # ------------------------------------------------------------------
    # Access control
    # ------------------------------------------------------------------
    def _is_admin(self, update: Update) -> bool:
        uid = update.effective_user.id if update.effective_user else None
        return uid in self.settings.admin_ids

    def _require_2fa(self, update: Update) -> bool:
        """True if the user has an active (unlocked) 2FA session."""
        uid = update.effective_user.id
        return self.sessions.is_unlocked(uid)

    async def _deny(self, update: Update, reason: str) -> None:
        await update.message.reply_text(f"⛔ {reason}")

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------
    def _register_handlers(self) -> None:
        add = self.app.add_handler
        add(CommandHandler("start", self._cmd_start))
        add(CommandHandler("help", self._cmd_help))
        add(CommandHandler("setup_2fa", self._cmd_setup_2fa))
        add(CommandHandler("auth", self._cmd_auth))
        # read-only
        add(CommandHandler("status", self._cmd_status))
        add(CommandHandler("positions", self._cmd_positions))
        add(CommandHandler("pnl", self._cmd_pnl))
        # privileged (2FA)
        add(CommandHandler("start_engine", self._cmd_start_engine))
        add(CommandHandler("stop_engine", self._cmd_stop_engine))
        add(CommandHandler("pause", self._cmd_pause))
        add(CommandHandler("resume", self._cmd_resume))
        add(CommandHandler("kill", self._cmd_kill))

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return await self._deny(update, "You are not an authorized admin.")
        await update.message.reply_text(
            "🤖 *Bybit Algo Bot*\n\n"
            "Use /setup_2fa to bind an authenticator, then /auth <code> to unlock.\n"
            "Run /help to see all commands.",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        await update.message.reply_text(
            "*Commands*\n"
            "🔐 /setup_2fa — bind authenticator (QR)\n"
            "🔓 /auth `<6-digit>` — unlock privileged commands\n\n"
            "👁 /status — engine state & equity\n"
            "👁 /positions — open positions\n"
            "👁 /pnl — profit & loss\n\n"
            "⚙️ /start_engine — start trading `[2FA]`\n"
            "⚙️ /stop_engine — graceful stop `[2FA]`\n"
            "⚙️ /pause — pause orders `[2FA]`\n"
            "⚙️ /resume — resume orders `[2FA]`\n"
            "🛑 /kill — EMERGENCY flatten `[2FA]`",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _cmd_setup_2fa(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        secret = self.settings.totp_secret or generate_secret()
        if not self.settings.totp_secret:
            await update.message.reply_text(
                "⚠️ No TOTP_SECRET in env. Generated a *new* secret — "
                "store it in your `.env` as `TOTP_SECRET`:\n\n"
                f"`{secret}`",
                parse_mode=ParseMode.MARKDOWN,
            )
        uri = provisioning_uri(secret, label=update.effective_user.username or "admin")
        try:
            import qrcode

            img = qrcode.make(uri)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            await update.message.reply_photo(
                buf, caption="Scan with Google Authenticator / Authy."
            )
        except Exception as exc:
            await update.message.reply_text(f"otpauth URI (no QR lib):\n{uri}\n\nerr: {exc}")

    async def _cmd_auth(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        if not ctx.args:
            return await update.message.reply_text("Usage: /auth <6-digit-code>")
        code = ctx.args[0]
        secret = self.settings.totp_secret
        if not secret:
            return await self._deny(update, "No TOTP_SECRET configured. Run /setup_2fa first.")
        if verify(secret, code):
            self.sessions.unlock(update.effective_user.id, self.settings.session_ttl)
            mins = self.settings.session_ttl // 60
            await update.message.reply_text(f"✅ Unlocked for {mins} min.")
        else:
            n = self.sessions.record_failure(update.effective_user.id)
            self._log.warning("2fa_failed", user=update.effective_user.id, attempts=n)
            await self._deny(update, f"Invalid code (attempt {n}).")

    # ---- read-only commands ----------------------------------------
    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        st = await self.engine.status()
        stats = st["stats"]
        text = (
            f"*Status*\n"
            f"• State: `{st['state']}` ({st['mode']})\n"
            f"• Symbol: `{st['symbol']}`  Lev: `{st['leverage']}x`\n"
            f"• Equity: `{st['equity_usdt']} USDT`\n"
            f"• Mid: `{st['mid_price']}`\n"
            f"• Candles buffered: `{st['candles_buffered']}`\n"
            f"• Signals: `{stats['signals']}`  "
            f"Orders: `{stats['orders_placed']}` "
            f"(filled {stats['orders_filled']}, failed {stats['orders_failed']})\n"
            f"• Since: `{stats['uptime_since']}`"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    async def _cmd_positions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        st = await self.engine.status()
        positions = st["positions"]
        if not positions:
            return await update.message.reply_text("No open positions.")
        lines = []
        for p in positions:
            if float(p.get("size", 0)) == 0:
                continue
            lines.append(
                f"• {p.get('symbol')} {p.get('side')} size={p.get('size')} "
                f"entry={p.get('avgPrice')} uPnL={p.get('unrealisedPnl')}"
            )
        await update.message.reply_text("\n".join(lines) or "No open positions.")

    async def _cmd_pnl(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        st = await self.engine.status()
        total_upnl = 0.0
        for p in st["positions"]:
            total_upnl += float(p.get("unrealisedPnl", 0) or 0)
        await update.message.reply_text(
            f"Unrealized PnL: `{round(total_upnl, 2)} USDT`\n"
            f"Equity: `{st['equity_usdt']} USDT`",
            parse_mode=ParseMode.MARKDOWN,
        )

    # ---- privileged commands (require 2FA) -------------------------
    async def _guard(self, update: Update) -> bool:
        """Return True if the caller passed the 2FA gate."""
        if not self._is_admin(update):
            await self._deny(update, "Not authorized.")
            return False
        if not self._require_2fa(update):
            await self._deny(update, "2FA required. Run /auth <code> first.")
            return False
        return True

    async def _cmd_start_engine(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await self.engine.start()
        await update.message.reply_text("▶️ Engine started.")

    async def _cmd_stop_engine(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await self.engine.stop()
        await update.message.reply_text("⏹ Engine stopped (positions kept).")

    async def _cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await self.engine.pause()
        await update.message.reply_text("⏸ Paused (streaming, no orders).")

    async def _cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await self.engine.resume()
        await update.message.reply_text("▶️ Resumed.")

    async def _cmd_kill(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        msg = await self.engine.kill_switch()
        await update.message.reply_text(msg)

    # ------------------------------------------------------------------
    # Broadcast / alerts (O3)
    # ------------------------------------------------------------------
    async def _broadcast(self, message: str) -> None:
        """Push an alert to every whitelisted admin (used by bot.alerts)."""
        for uid in self.settings.admin_ids:
            try:
                await self.app.bot.send_message(uid, message)
            except Exception as exc:
                self._log.warning("broadcast_failed", uid=uid, error=str(exc))

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Start polling. Blocks until stopped."""
        await self.app.initialize()
        await self.app.start()
        # Register this bot as the global alert sender (O3).
        set_alert_sender(self._broadcast)
        self._log.info("telegram_bot_started")
        await self.app.updater.start_polling()

    async def stop(self) -> None:
        set_alert_sender(None)
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
        self._log.info("telegram_bot_stopped")
