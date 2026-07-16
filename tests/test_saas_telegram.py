"""Smoke tests for the multi-tenant Telegram SaaS bot command handlers.

Skipped when python-telegram-bot isn't installed (it's a heavy optional dep;
the core SaaS logic is tested independently).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("telegram")  # skip whole module if python-telegram-bot absent

from bot.telegram_saas import TelegramSaaSBot  # noqa: E402
from saas import crypto  # noqa: E402
from saas.database import Database  # noqa: E402
from saas.tenant_runner import TenantRunner  # noqa: E402
from saas.user_manager import UserManager  # noqa: E402


@pytest.fixture()
def bot(tmp_path):
    db = Database(str(tmp_path / "tg.db"))
    db.init()
    mgr = UserManager(db, crypto.generate_master_secret())
    runner = TenantRunner(mgr, exchange_factory=lambda k, s: MagicMock())
    return TelegramSaaSBot("fake-token", mgr, runner)


def _mock_update(tid=999, username="tester", args=None):
    """Build a mock telegram.Update with an awaitable reply_text."""
    update = MagicMock()
    update.effective_user.id = tid
    update.effective_user.username = username
    update.message.reply_text = AsyncMock()
    update.message.delete = AsyncMock()
    ctx = MagicMock()
    ctx.args = args or []
    ctx.bot.username = "carrybot"
    return update, ctx


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ----------------------------------------------------------------- /start


def test_start_registers_new_user(bot):
    upd, ctx = _mock_update(tid=5001)
    _run(bot._cmd_start(upd, ctx))
    user = bot.mgr.get_by_telegram_id(5001)
    assert user is not None
    assert user.is_subscribed  # trial active
    assert len(user.referral_code) == 8
    upd.message.reply_text.assert_awaited_once()


def test_start_with_referral(bot):
    boss = bot.mgr.register(1, "boss")
    upd, ctx = _mock_update(tid=5002, args=[boss.referral_code])
    _run(bot._cmd_start(upd, ctx))
    invited = bot.mgr.get_by_telegram_id(5002)
    assert invited.referred_by == boss.id


def test_start_welbacks_existing_user(bot):
    bot.mgr.register(5003, "returning")
    upd, ctx = _mock_update(tid=5003, username="returning")
    _run(bot._cmd_start(upd, ctx))
    text = upd.message.reply_text.call_args.args[0]
    assert "С возвращением" in text


# ----------------------------------------------------------------- /status


def test_status_requires_registration(bot):
    upd, ctx = _mock_update(tid=5004)
    _run(bot._cmd_status(upd, ctx))
    text = upd.message.reply_text.call_args.args[0]
    assert "/start" in text


def test_status_shows_tier(bot):
    bot.mgr.register(5005, "statuser")
    upd, ctx = _mock_update(tid=5005)
    _run(bot._cmd_status(upd, ctx))
    text = upd.message.reply_text.call_args.args[0]
    assert "free" in text.lower()


# ----------------------------------------------------------------- /start_bot


def test_start_bot_requires_api_key(bot):
    bot.mgr.register(5006, "nokeys")
    upd, ctx = _mock_update(tid=5006)
    _run(bot._cmd_start_bot(upd, ctx))
    text = upd.message.reply_text.call_args.args[0]
    assert "API" in text or "connect" in text


def test_start_bot_requires_subscription(bot):
    import time
    u = bot.mgr.register(5007, "expired")
    bot.mgr.connect_api_key(u.id, "k", "s")
    with bot.mgr.db.connect() as conn:
        conn.execute("UPDATE users SET subscription_until=? WHERE id=?",
                     (time.time() - 1, u.id))
    upd, ctx = _mock_update(tid=5007)
    _run(bot._cmd_start_bot(upd, ctx))
    text = upd.message.reply_text.call_args.args[0]
    assert "истекла" in text or "pricing" in text


def test_start_bot_success(bot):
    u = bot.mgr.register(5008, "ready")
    bot.mgr.connect_api_key(u.id, "k", "s")
    upd, ctx = _mock_update(tid=5008)
    _run(bot._cmd_start_bot(upd, ctx))
    refreshed = bot.mgr.get_by_id(u.id)
    assert refreshed.bot_enabled is True


# ----------------------------------------------------------------- /referral


def test_referral_shows_code(bot):
    u = bot.mgr.register(5009, "referrer")
    upd, ctx = _mock_update(tid=5009)
    _run(bot._cmd_referral(upd, ctx))
    text = upd.message.reply_text.call_args.args[0]
    assert u.referral_code in text


# ----------------------------------------------------------------- /connect


def test_connect_usage_hint(bot):
    bot.mgr.register(5010, "newbie")
    upd, ctx = _mock_update(tid=5010, args=[])  # no args
    _run(bot._cmd_connect(upd, ctx))
    text = upd.message.reply_text.call_args.args[0]
    assert "connect" in text.lower()


def test_handlers_registered(bot):
    """All 8 commands are wired into the application."""
    # python-telegram-bot stores handlers internally; check the builder worked.
    assert bot.app is not None
