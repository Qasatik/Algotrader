#!/usr/bin/env python3
"""Launch the multi-tenant SaaS Telegram bot (consolidated).

This is the single entry-point for the production SaaS bot.  It wires up
the full stack — database, user manager, billing service, payment gateway,
tenant runner, and the Telegram poller — from environment variables.

Environment:
    TELEGRAM_BOT_TOKEN   — Telegram bot token from @BotFather
    SAAS_MASTER_SECRET   — AES-256 master secret for API-key encryption
                           (generate once: python3 -c "from saas.crypto import
                           generate_master_secret as g; print(g())")
    SAAS_DB_PATH         — SQLite path (default data/saas.db)
    SAAS_ADMIN_IDS       — comma-separated Telegram user IDs of admins
    SAAS_USDT_WALLET     — USDT TRC-20 wallet for crypto payments
                           (if empty, falls back to manual admin confirmation)

Usage:
    env TELEGRAM_BOT_TOKEN=xxx SAAS_MASTER_SECRET=yyy \
        SAAS_ADMIN_IDS=123456 SAAS_USDT_WALLET=Txxx \
        python3 scripts/run_saas_bot.py
"""

import asyncio
import os
import sys

from saas.billing import BillingService, ManualGateway, UsdtGateway
from saas.database import DEFAULT_DB_PATH, Database
from saas.telegram_saas import SaaSTelegramBot
from saas.tenant_runner import TenantRunner
from saas.user_manager import UserManager


def build_from_env() -> SaaSTelegramBot:
    """Construct the full SaaS stack from environment variables."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    secret = os.environ["SAAS_MASTER_SECRET"]
    db_path = os.environ.get("SAAS_DB_PATH", DEFAULT_DB_PATH)
    admin_ids = [
        int(x) for x in os.environ.get("SAAS_ADMIN_IDS", "").split(",") if x.strip()
    ]
    usdt_wallet = os.environ.get("SAAS_USDT_WALLET", "")

    db = Database(db_path)
    db.init()
    mgr = UserManager(db, secret)
    gateway = UsdtGateway(usdt_wallet) if usdt_wallet else ManualGateway()
    billing = BillingService(db, mgr, gateway)
    runner = TenantRunner(mgr)

    return SaaSTelegramBot(
        token, mgr, billing, admin_ids=admin_ids, runner=runner,
    )


def main() -> None:
    try:
        bot = build_from_env()
    except KeyError as exc:
        print(f"❌ Missing env var: {exc}", file=sys.stderr)
        print(
            "Required: TELEGRAM_BOT_TOKEN, SAAS_MASTER_SECRET\n"
            "Optional: SAAS_DB_PATH, SAAS_ADMIN_IDS, SAAS_USDT_WALLET",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        print("\nbye 👋")


if __name__ == "__main__":
    main()
