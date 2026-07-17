"""Tests for the SaaS user-facing Telegram bot (saas/telegram_saas.py).

Uses lightweight mock objects for ``Update`` / ``ContextTypes`` so we can
test the command handlers without a real Telegram connection.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from core.security import ApiKeyAudit
from saas.billing import BillingService, UsdtGateway
from saas.database import Database
from saas.models import Tier
from saas.telegram_saas import SaaSTelegramBot, _md_to_html
from saas.user_manager import UserManager

_MASTER = "test-master-secret-for-unit-tests"


# ═══════════════════════════════════════════════════════════════════════════
# Mock Telegram objects
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class _MockUser:
    id: int
    username: str = ""


@dataclass
class _MockMessage:
    text: str = ""
    replies: list[str] = field(default_factory=list)
    reply_markups: list = field(default_factory=list)
    deleted: bool = False

    async def reply_text(self, text: str, parse_mode=None, reply_markup=None) -> None:
        self.replies.append(text)
        self.reply_markups.append(reply_markup)

    async def delete(self) -> None:
        self.deleted = True


@dataclass
class _MockCallbackQuery:
    data: str = ""
    answered: bool = False

    async def answer(self) -> None:
        self.answered = True


@dataclass
class _MockBot:
    username: str = "algotrader_test_bot"


@dataclass
class _MockContext:
    args: list[str] = field(default_factory=list)
    bot: _MockBot = field(default_factory=_MockBot)


@dataclass
class _MockUpdateContainer:
    effective_user: _MockUser
    message: _MockMessage
    callback_query: _MockCallbackQuery | None = None


def _make_update(uid: int, username: str = "tester") -> tuple:
    """Return ``(update, ctx)`` mock pair for a given Telegram user."""
    msg = _MockMessage()
    update = _MockUpdateContainer(
        effective_user=_MockUser(id=uid, username=username),
        message=msg,
    )
    ctx = _MockContext()
    return update, ctx


def _make_callback_update(uid: int, callback_data: str, username: str = "tester") -> tuple:
    """Mock update for an inline-button press (callback query)."""
    msg = _MockMessage()
    cq = _MockCallbackQuery(data=callback_data)
    update = _MockUpdateContainer(
        effective_user=_MockUser(id=uid, username=username),
        message=msg,
        callback_query=cq,
    )
    ctx = _MockContext()
    return update, ctx


def _make_text_update(uid: int, text: str, username: str = "tester") -> tuple:
    """Mock update for a reply-keyboard button press (text message)."""
    msg = _MockMessage(text=text)
    update = _MockUpdateContainer(
        effective_user=_MockUser(id=uid, username=username),
        message=msg,
    )
    ctx = _MockContext()
    return update, ctx


def _run(coro):
    """Run an async coroutine synchronously (no pytest-asyncio needed)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("loop closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def bot(tmp_path):
    """A SaaSTelegramBot wired to a temp DB + USDT gateway."""
    db = Database(str(tmp_path / "saas.db"))
    db.init()
    mgr = UserManager(db, _MASTER)
    gateway = UsdtGateway("TTestWallet123")
    billing = BillingService(db, mgr, gateway)
    return SaaSTelegramBot(
        token="123:test-token", mgr=mgr, billing=billing,
        admin_ids=[999],
    )


# ═══════════════════════════════════════════════════════════════════════════
# /start — registration & greeting
# ═══════════════════════════════════════════════════════════════════════════


class TestStart:
    def test_registers_new_user(self, bot):
        update, ctx = _make_update(111, "alice")
        _run(bot._cmd_start(update, ctx))
        user = bot.mgr.get_by_telegram_id(111)
        assert user is not None
        assert user.username == "alice"
        assert user.tier == Tier.FREE
        assert "AlgoTrader" in update.message.replies[0]

    def test_greets_returning_user(self, bot):
        # First /start registers.
        update, ctx = _make_update(222, "bob")
        _run(bot._cmd_start(update, ctx))
        # Second /start greets (no duplicate).
        update2, ctx2 = _make_update(222, "bob")
        _run(bot._cmd_start(update2, ctx2))
        user = bot.mgr.get_by_telegram_id(222)
        assert user is not None
        assert "AlgoTrader" in update2.message.replies[0]

    def test_referral_deep_link(self, bot):
        # Alice registers first, gets a referral code.
        u_alice, _ = _make_update(111, "alice")
        _run(bot._cmd_start(u_alice, _MockContext()))
        alice = bot.mgr.get_by_telegram_id(111)
        assert alice is not None
        assert alice.referral_code

        # Bob starts with Alice's referral code.
        u_bob, ctx_bob = _make_update(222, "bob")
        ctx_bob.args = [f"ref_{alice.referral_code}"]
        _run(bot._cmd_start(u_bob, ctx_bob))
        bob = bot.mgr.get_by_telegram_id(222)
        assert bob is not None
        assert bob.referred_by == alice.id


# ═══════════════════════════════════════════════════════════════════════════
# /pricing
# ═══════════════════════════════════════════════════════════════════════════


class TestPricing:
    def test_shows_all_tiers(self, bot):
        update, ctx = _make_update(111)
        _run(bot._cmd_pricing(update, ctx))
        text = update.message.replies[0]
        assert "BASIC" in text
        assert "PRO" in text
        assert "VIP" in text
        assert "3" in text   # $3 basic
        assert "8" in text   # $8 pro
        assert "15" in text  # $15 vip


# ═══════════════════════════════════════════════════════════════════════════
# /subscribe
# ═══════════════════════════════════════════════════════════════════════════


class TestSubscribe:
    def test_creates_invoice_for_pro(self, bot):
        update, ctx = _make_update(111)
        ctx.args = ["pro"]
        _run(bot._cmd_subscribe(update, ctx))
        text = update.message.replies[0]
        assert "Счёт" in text
        assert "PRO" in text
        # Invoice should exist in DB.
        inv = bot.billing.pending_invoice(
            bot.mgr.get_by_telegram_id(111).id
        )
        assert inv is not None
        assert inv.plan == "pro"
        assert inv.amount_usdt == 8.0

    def test_creates_invoice_for_basic(self, bot):
        update, ctx = _make_update(111)
        ctx.args = ["basic"]
        _run(bot._cmd_subscribe(update, ctx))
        inv = bot.billing.pending_invoice(
            bot.mgr.get_by_telegram_id(111).id
        )
        assert inv is not None
        assert inv.amount_usdt == 3.0

    def test_no_args_shows_usage(self, bot):
        update, ctx = _make_update(111)
        ctx.args = []
        _run(bot._cmd_subscribe(update, ctx))
        assert "Укажите тариф" in update.message.replies[0]

    def test_free_tier_rejected(self, bot):
        update, ctx = _make_update(111)
        ctx.args = ["free"]
        _run(bot._cmd_subscribe(update, ctx))
        assert "FREE" in update.message.replies[0]
        assert "бесплатный" in update.message.replies[0].lower()

    def test_invalid_tier_rejected(self, bot):
        update, ctx = _make_update(111)
        ctx.args = ["diamond"]
        _run(bot._cmd_subscribe(update, ctx))
        assert "Неизвестный тариф" in update.message.replies[0]

    def test_invoice_has_usdt_instructions(self, bot):
        update, ctx = _make_update(111)
        ctx.args = ["vip"]
        _run(bot._cmd_subscribe(update, ctx))
        text = update.message.replies[0]
        assert "TRC-20" in text
        assert "TTestWallet123" in text


# ═══════════════════════════════════════════════════════════════════════════
# /pay
# ═══════════════════════════════════════════════════════════════════════════


class TestPay:
    def test_shows_pending_invoice(self, bot):
        # Register + create invoice.
        u1, c1 = _make_update(111)
        _run(bot._cmd_start(u1, _MockContext()))
        c1.args = ["pro"]
        _run(bot._cmd_subscribe(u1, c1))

        # /pay shows the pending invoice.
        u2, c2 = _make_update(111)
        _run(bot._cmd_pay(u2, c2))
        text = u2.message.replies[0]
        assert "Счёт" in text
        assert "PRO" in text
        assert "TRC-20" in text

    def test_no_pending_invoice(self, bot):
        update, ctx = _make_update(111)
        _run(bot._cmd_start(update, _MockContext()))
        _run(bot._cmd_pay(update, ctx))
        assert "нет ожидающих" in update.message.replies[-1].lower()


# ═══════════════════════════════════════════════════════════════════════════
# /invoices
# ═══════════════════════════════════════════════════════════════════════════


class TestInvoices:
    def test_empty_invoices(self, bot):
        update, ctx = _make_update(111)
        _run(bot._cmd_start(update, _MockContext()))
        _run(bot._cmd_invoices(update, ctx))
        assert "нет счетов" in update.message.replies[-1].lower()

    def test_lists_created_invoice(self, bot):
        u1, c1 = _make_update(111)
        _run(bot._cmd_start(u1, _MockContext()))
        c1.args = ["pro"]
        _run(bot._cmd_subscribe(u1, c1))

        u2, c2 = _make_update(111)
        _run(bot._cmd_invoices(u2, c2))
        text = u2.message.replies[-1]
        assert "PRO" in text
        assert "8" in text  # $8


# ═══════════════════════════════════════════════════════════════════════════
# /myplan
# ═══════════════════════════════════════════════════════════════════════════


class TestMyPlan:
    def test_shows_free_tier(self, bot):
        update, ctx = _make_update(111)
        _run(bot._cmd_start(update, _MockContext()))
        _run(bot._cmd_myplan(update, ctx))
        text = update.message.replies[-1]
        assert "FREE" in text
        assert "100" in text  # max_notional for free
        assert "не подключён" in text  # no API key

    def test_shows_paid_tier_after_upgrade(self, bot):
        u1, c1 = _make_update(111)
        _run(bot._cmd_start(u1, _MockContext()))
        user = bot.mgr.get_by_telegram_id(111)

        # Simulate payment → PRO.
        inv = bot.billing.create_invoice(user.id, Tier.PRO)
        bot.billing.confirm_payment(inv.id, "tx_test")

        u2, c2 = _make_update(111)
        _run(bot._cmd_myplan(u2, c2))
        text = u2.message.replies[-1]
        assert "PRO" in text
        assert "5000" in text  # max_notional for PRO


# ═══════════════════════════════════════════════════════════════════════════
# /referral
# ═══════════════════════════════════════════════════════════════════════════


class TestReferral:
    def test_shows_referral_code(self, bot):
        update, ctx = _make_update(111, "alice")
        _run(bot._cmd_start(update, _MockContext()))
        _run(bot._cmd_referral(update, ctx))
        text = update.message.replies[-1]
        user = bot.mgr.get_by_telegram_id(111)
        assert user.referral_code in text
        assert "algotrader_test_bot" in text  # bot username in link
        assert "0" in text  # 0 invited, 0 earned

    def test_shows_earnings_after_referral_pays(self, bot):
        # Alice registers.
        u_a, _ = _make_update(111, "alice")
        _run(bot._cmd_start(u_a, _MockContext()))
        alice = bot.mgr.get_by_telegram_id(111)

        # Bob registers with Alice's code.
        u_b, c_b = _make_update(222, "bob")
        c_b.args = [f"ref_{alice.referral_code}"]
        _run(bot._cmd_start(u_b, c_b))
        bob = bot.mgr.get_by_telegram_id(222)

        # Bob pays.
        inv = bot.billing.create_invoice(bob.id, Tier.PRO)
        bot.billing.confirm_payment(inv.id, "tx1")

        # Alice checks referral.
        u_a2, c_a2 = _make_update(111, "alice")
        _run(bot._cmd_referral(u_a2, c_a2))
        text = u_a2.message.replies[-1]
        assert "1" in text  # 1 invited
        assert "0.80" in text  # 10% of $8 = $0.80


# ═══════════════════════════════════════════════════════════════════════════
# /admin_pay
# ═══════════════════════════════════════════════════════════════════════════


class TestAdminPay:
    def test_admin_confirms_payment(self, bot):
        # User creates invoice.
        u, c = _make_update(111)
        _run(bot._cmd_start(u, _MockContext()))
        user = bot.mgr.get_by_telegram_id(111)
        inv = bot.billing.create_invoice(user.id, Tier.BASIC)

        # Admin confirms.
        a, ca = _make_update(999, "admin")  # 999 is admin
        ca.args = [str(inv.id)]
        _run(bot._cmd_admin_pay(a, ca))
        text = a.message.replies[-1]
        assert "оплачен" in text.lower()
        assert "BASIC" in text

        # Verify subscription extended.
        refreshed = bot.mgr.get_by_id(user.id)
        assert refreshed.tier == Tier.BASIC
        assert refreshed.is_subscribed

    def test_non_admin_denied(self, bot):
        u, c = _make_update(111)
        _run(bot._cmd_start(u, _MockContext()))
        user = bot.mgr.get_by_telegram_id(111)
        inv = bot.billing.create_invoice(user.id, Tier.PRO)

        # Non-admin (uid=222) tries to confirm.
        a, ca = _make_update(222, "hacker")
        ca.args = [str(inv.id)]
        _run(bot._cmd_admin_pay(a, ca))
        assert "администратора" in a.message.replies[-1]

        # Invoice should still be pending.
        check = bot.billing.get_invoice(inv.id)
        assert check.status.value == "pending"

    def test_no_args_shows_usage(self, bot):
        a, ca = _make_update(999, "admin")
        ca.args = []
        _run(bot._cmd_admin_pay(a, ca))
        assert "Использование" in a.message.replies[-1]

    def test_non_numeric_id(self, bot):
        a, ca = _make_update(999, "admin")
        ca.args = ["abc"]
        _run(bot._cmd_admin_pay(a, ca))
        assert "числом" in a.message.replies[-1]

    def test_nonexistent_invoice(self, bot):
        a, ca = _make_update(999, "admin")
        ca.args = ["99999"]
        _run(bot._cmd_admin_pay(a, ca))
        assert "не найден" in a.message.replies[-1]

    def test_double_confirm_idempotent(self, bot):
        u, c = _make_update(111)
        _run(bot._cmd_start(u, _MockContext()))
        user = bot.mgr.get_by_telegram_id(111)
        inv = bot.billing.create_invoice(user.id, Tier.PRO)

        # First confirm.
        a1, ca1 = _make_update(999, "admin")
        ca1.args = [str(inv.id)]
        _run(bot._cmd_admin_pay(a1, ca1))
        assert "оплачен" in a1.message.replies[-1].lower()

        # Second confirm → "already processed".
        a2, ca2 = _make_update(999, "admin")
        ca2.args = [str(inv.id)]
        _run(bot._cmd_admin_pay(a2, ca2))
        assert "уже был обработан" in a2.message.replies[-1]


# ═══════════════════════════════════════════════════════════════════════════
# /help
# ═══════════════════════════════════════════════════════════════════════════


class TestHelp:
    def test_lists_commands(self, bot):
        update, ctx = _make_update(111)
        _run(bot._cmd_start(update, _MockContext()))
        _run(bot._cmd_help(update, ctx))
        text = update.message.replies[-1]
        assert "/pricing" in text
        assert "/subscribe" in text
        assert "/referral" in text

    def test_admin_sees_admin_commands(self, bot):
        update, ctx = _make_update(999, "admin")
        _run(bot._cmd_start(update, _MockContext()))
        _run(bot._cmd_help(update, ctx))
        text = update.message.replies[-1]
        assert "/admin_pay" in text

    def test_non_admin_no_admin_commands(self, bot):
        update, ctx = _make_update(111)
        _run(bot._cmd_start(update, _MockContext()))
        _run(bot._cmd_help(update, ctx))
        text = update.message.replies[-1]
        assert "/admin_pay" not in text


# ═══════════════════════════════════════════════════════════════════════════
# Utility: _md_to_html
# ═══════════════════════════════════════════════════════════════════════════


class TestMdToHtml:
    def test_converts_backticks(self):
        assert _md_to_html("`hello`") == "<code>hello</code>"

    def test_converts_multiple(self):
        result = _md_to_html("addr: `abc` memo: `xyz`")
        assert "<code>abc</code>" in result
        assert "<code>xyz</code>" in result

    def test_no_backticks_unchanged(self):
        assert _md_to_html("plain text") == "plain text"


# ═══════════════════════════════════════════════════════════════════════════
# Keyboards (inline + reply)
# ═══════════════════════════════════════════════════════════════════════════


class TestKeyboards:
    def test_main_keyboard_has_buttons(self, bot):
        kb = bot._main_keyboard()
        # ReplyKeyboardMarkup has .keyboard attribute
        assert kb is not None
        assert len(kb.keyboard) >= 2

    def test_pricing_keyboard_has_tiers(self, bot):
        kb = bot._pricing_keyboard()
        assert kb is not None
        # InlineKeyboardMarkup has .inline_keyboard
        buttons = [btn for row in kb.inline_keyboard for btn in row]
        texts = [b.text for b in buttons]
        assert any("BASIC" in t for t in texts)
        assert any("PRO" in t for t in texts)
        assert any("VIP" in t for t in texts)
        # Check callback_data
        datas = [b.callback_data for b in buttons]
        assert "subscribe:basic" in datas
        assert "subscribe:pro" in datas
        assert "subscribe:vip" in datas

    def test_start_includes_main_keyboard(self, bot):
        update, ctx = _make_update(111)
        _run(bot._cmd_start(update, ctx))
        # The last reply should have a reply_markup (main keyboard).
        assert update.message.reply_markups[-1] is not None

    def test_pricing_includes_inline_keyboard(self, bot):
        update, ctx = _make_update(111)
        _run(bot._cmd_pricing(update, ctx))
        markup = update.message.reply_markups[-1]
        assert markup is not None
        # Should be an InlineKeyboardMarkup with tier buttons.
        buttons = [btn for row in markup.inline_keyboard for btn in row]
        assert any(b.callback_data == "subscribe:pro" for b in buttons)


# ═══════════════════════════════════════════════════════════════════════════
# Callback handler (inline button presses)
# ═══════════════════════════════════════════════════════════════════════════


class TestCallbackHandler:
    def test_subscribe_callback_creates_invoice(self, bot):
        # Register first.
        u, c = _make_update(111)
        _run(bot._cmd_start(u, _MockContext()))

        # Press "subscribe:pro" inline button.
        cb, cc = _make_callback_update(111, "subscribe:pro")
        _run(bot._handle_callback(cb, cc))

        assert cb.callback_query.answered  # query.answer() was called
        text = cb.message.replies[-1]
        assert "PRO" in text
        assert "Счёт" in text

    def test_pay_callback(self, bot):
        u, c = _make_update(111)
        _run(bot._cmd_start(u, _MockContext()))
        c.args = ["basic"]
        _run(bot._cmd_subscribe(u, c))

        cb, cc = _make_callback_update(111, "pay")
        _run(bot._handle_callback(cb, cc))
        text = cb.message.replies[-1]
        assert "Счёт" in text

    def test_unknown_callback_ignored(self, bot):
        u, c = _make_update(111)
        _run(bot._cmd_start(u, _MockContext()))

        cb, cc = _make_callback_update(111, "unknown_action")
        _run(bot._handle_callback(cb, cc))
        # Should answer the query but not crash.
        assert cb.callback_query.answered


# ═══════════════════════════════════════════════════════════════════════════
# Menu text handler (reply keyboard buttons)
# ═══════════════════════════════════════════════════════════════════════════


class TestMenuText:
    def test_pricing_button(self, bot):
        u, c = _make_update(111)
        _run(bot._cmd_start(u, _MockContext()))

        tu, tc = _make_text_update(111, "💰 Тарифы")
        _run(bot._handle_menu_text(tu, tc))
        text = tu.message.replies[-1]
        assert "Тарифы" in text
        assert "BASIC" in text

    def test_myplan_button(self, bot):
        u, c = _make_update(111)
        _run(bot._cmd_start(u, _MockContext()))

        tu, tc = _make_text_update(111, "📋 Мой тариф")
        _run(bot._handle_menu_text(tu, tc))
        text = tu.message.replies[-1]
        assert "FREE" in text

    def test_referral_button(self, bot):
        u, c = _make_update(111, "alice")
        _run(bot._cmd_start(u, _MockContext()))

        tu, tc = _make_text_update(111, "🎁 Рефералка")
        _run(bot._handle_menu_text(tu, tc))
        text = tu.message.replies[-1]
        assert "Реферальная" in text

    def test_help_button(self, bot):
        u, c = _make_update(111)
        _run(bot._cmd_start(u, _MockContext()))

        tu, tc = _make_text_update(111, "❓ Помощь")
        _run(bot._handle_menu_text(tu, tc))
        text = tu.message.replies[-1]
        assert "/pricing" in text

    def test_unrecognized_text_ignored(self, bot):
        u, c = _make_update(111)
        _run(bot._cmd_start(u, _MockContext()))

        tu, tc = _make_text_update(111, "random text")
        _run(bot._handle_menu_text(tu, tc))
        # No replies should be added (handler ignores unknown text).
        assert len(tu.message.replies) == 0


# ═══════════════════════════════════════════════════════════════════════════
# /connect — API key connection (audited, encrypted)
# ═══════════════════════════════════════════════════════════════════════════


def _ok_audit() -> ApiKeyAudit:
    """A passing API-key audit (safe permissions, IP-whitelisted)."""
    return ApiKeyAudit(
        ok=True, note="test-key",
        permissions={"ContractTrade": ["Order"]},
        ip_whitelist=["1.2.3.4"], expires_at=None, read_only=False,
    )


def _bad_audit() -> ApiKeyAudit:
    """A failing API-key audit (dangerous withdrawal permission)."""
    return ApiKeyAudit(
        ok=False, note="bad-key", permissions={},
        ip_whitelist=[], expires_at=None, read_only=False,
        warnings=["CRITICAL: key has 'Withdraw' permission"],
    )


class TestConnect:
    def test_no_args_shows_usage(self, bot):
        u, c = _make_update(111)
        _run(bot._cmd_start(u, _MockContext()))

        u2, c2 = _make_update(111)
        _run(bot._cmd_connect(u2, c2))
        assert "Использование" in u2.message.replies[-1]

    def test_successful_connect(self, bot):
        u, c = _make_update(111)
        _run(bot._cmd_start(u, _MockContext()))

        with patch("saas.telegram_saas.audit_api_key", return_value=_ok_audit()):
            u2, c2 = _make_update(111)
            c2.args = ["fakekey", "fakesecret"]
            _run(bot._cmd_connect(u2, c2))

        text = u2.message.replies[-1]
        assert "подключён" in text.lower()
        user = bot.mgr.get_by_telegram_id(111)
        assert user.has_api_key

    def test_rejected_key_not_stored(self, bot):
        u, c = _make_update(111)
        _run(bot._cmd_start(u, _MockContext()))

        with patch("saas.telegram_saas.audit_api_key", return_value=_bad_audit()):
            u2, c2 = _make_update(111)
            c2.args = ["badkey", "badsecret"]
            _run(bot._cmd_connect(u2, c2))

        text = u2.message.replies[-1]
        assert "отклонён" in text.lower()
        user = bot.mgr.get_by_telegram_id(111)
        assert not user.has_api_key

    def test_audit_failure_shows_error(self, bot):
        u, c = _make_update(111)
        _run(bot._cmd_start(u, _MockContext()))

        with patch("saas.telegram_saas.audit_api_key", side_effect=RuntimeError("network")):
            u2, c2 = _make_update(111)
            c2.args = ["key", "secret"]
            _run(bot._cmd_connect(u2, c2))

        text = u2.message.replies[-1]
        assert "Не удалось" in text
        user = bot.mgr.get_by_telegram_id(111)
        assert not user.has_api_key

    def test_message_deleted_to_hide_secret(self, bot):
        u, c = _make_update(111)
        _run(bot._cmd_start(u, _MockContext()))

        with patch("saas.telegram_saas.audit_api_key", return_value=_ok_audit()):
            u2, c2 = _make_update(111)
            c2.args = ["key", "secret"]
            _run(bot._cmd_connect(u2, c2))

        assert u2.message.deleted


# ═══════════════════════════════════════════════════════════════════════════
# /start_bot, /stop_bot — trading toggle
# ═══════════════════════════════════════════════════════════════════════════


class TestStartBot:
    def test_no_api_key_rejected(self, bot):
        u, c = _make_update(111)
        _run(bot._cmd_start(u, _MockContext()))

        u2, c2 = _make_update(111)
        _run(bot._cmd_start_bot(u2, c2))
        assert "API-ключ" in u2.message.replies[-1]

    def test_expired_subscription_rejected(self, bot):
        u, c = _make_update(111)
        _run(bot._cmd_start(u, _MockContext()))
        user = bot.mgr.get_by_telegram_id(111)
        bot.mgr.connect_api_key(user.id, "k", "s")
        # Expire the 7-day trial so is_subscribed is False.
        with bot.mgr.db.connect() as conn:
            conn.execute(
                "UPDATE users SET subscription_until = 0 WHERE id = ?",
                (user.id,),
            )

        u2, c2 = _make_update(111)
        _run(bot._cmd_start_bot(u2, c2))
        assert "подписка" in u2.message.replies[-1].lower()

    def test_starts_with_key_and_subscription(self, bot):
        u, c = _make_update(111)
        _run(bot._cmd_start(u, _MockContext()))
        user = bot.mgr.get_by_telegram_id(111)
        bot.mgr.connect_api_key(user.id, "k", "s")
        inv = bot.billing.create_invoice(user.id, Tier.PRO)
        bot.billing.confirm_payment(inv.id, "tx1")

        u2, c2 = _make_update(111)
        _run(bot._cmd_start_bot(u2, c2))
        assert "запущен" in u2.message.replies[-1].lower()
        user = bot.mgr.get_by_telegram_id(111)
        assert user.bot_enabled


class TestStopBot:
    def test_stops_bot(self, bot):
        u, c = _make_update(111)
        _run(bot._cmd_start(u, _MockContext()))
        user = bot.mgr.get_by_telegram_id(111)
        bot.mgr.set_bot_enabled(user.id, True)

        u2, c2 = _make_update(111)
        _run(bot._cmd_stop_bot(u2, c2))
        assert "остановлен" in u2.message.replies[-1].lower()
        user = bot.mgr.get_by_telegram_id(111)
        assert not user.bot_enabled


# ═══════════════════════════════════════════════════════════════════════════
# /status — bot state overview
# ═══════════════════════════════════════════════════════════════════════════


class TestStatus:
    def test_shows_tier_and_subscription(self, bot):
        u, c = _make_update(111)
        _run(bot._cmd_start(u, _MockContext()))

        u2, c2 = _make_update(111)
        _run(bot._cmd_status(u2, c2))
        text = u2.message.replies[-1]
        assert "Статус" in text
        assert "FREE" in text
        assert "не подключён" in text  # no API key

    def test_shows_running_bot(self, bot):
        u, c = _make_update(111)
        _run(bot._cmd_start(u, _MockContext()))
        user = bot.mgr.get_by_telegram_id(111)
        bot.mgr.connect_api_key(user.id, "k", "s")
        inv = bot.billing.create_invoice(user.id, Tier.PRO)
        bot.billing.confirm_payment(inv.id, "tx1")
        bot.mgr.set_bot_enabled(user.id, True)

        u2, c2 = _make_update(111)
        _run(bot._cmd_status(u2, c2))
        text = u2.message.replies[-1]
        assert "работает" in text
        assert "подключён" in text
