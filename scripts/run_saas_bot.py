#!/usr/bin/env python3
"""Launch the multi-tenant SaaS Telegram bot.

Reads configuration from environment:
  TELEGRAM_BOT_TOKEN   — Telegram bot token from @BotFather
  SAAS_MASTER_SECRET   — AES-256 master secret for API-key encryption
                         (generate once: python3 -c "from saas.crypto import
                         generate_master_secret as g; print(g())")
  SAAS_DB_PATH         — SQLite path (default data/saas.db)

Usage:
  env TELEGRAM_BOT_TOKEN=xxx SAAS_MASTER_SECRET=yyy \
      python3 scripts/run_saas_bot.py
"""

import asyncio
import sys

from bot.telegram_saas import build_from_env


def main() -> None:
    try:
        bot = build_from_env()
    except KeyError as exc:
        print(f"❌ Missing env var: {exc}", file=sys.stderr)
        print("Required: TELEGRAM_BOT_TOKEN, SAAS_MASTER_SECRET", file=sys.stderr)
        sys.exit(1)
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        print("\nbye 👋")


if __name__ == "__main__":
    main()
