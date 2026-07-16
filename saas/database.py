"""SQLite database layer for the SaaS platform.

Schema + connection management only. Row ↔ model mapping lives in
:mod:`saas.user_manager`. SQLite is used for the MVP (zero-config, file-based);
the SQL is portable to PostgreSQL with minimal changes (swap ``INTEGER
PRIMARY KEY AUTOINCREMENT`` → ``SERIAL``, ``REAL`` → ``DOUBLE PRECISION``).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB_PATH = "data/saas.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id          INTEGER UNIQUE NOT NULL,
    username             TEXT    NOT NULL DEFAULT '',
    tier                 TEXT    NOT NULL DEFAULT 'free',
    subscription_until   REAL    NOT NULL DEFAULT 0,
    referral_code        TEXT    UNIQUE NOT NULL DEFAULT '',
    referred_by          INTEGER REFERENCES users(id),
    api_key_encrypted    TEXT,
    api_secret_encrypted TEXT,
    bot_enabled          INTEGER NOT NULL DEFAULT 0,
    created_at           REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan           TEXT    NOT NULL,
    amount         REAL    NOT NULL,
    paid_until     REAL    NOT NULL,
    payment_method TEXT    NOT NULL DEFAULT '',
    payment_id     TEXT    NOT NULL DEFAULT '',
    created_at     REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_configs (
    user_id          INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    top_n            INTEGER NOT NULL DEFAULT 1,
    equity_fraction  REAL    NOT NULL DEFAULT 0.5,
    max_notional     REAL,
    min_funding      REAL    NOT NULL DEFAULT 0.0001,
    leverage         INTEGER NOT NULL DEFAULT 2,
    stop_loss_pct    REAL    NOT NULL DEFAULT 15.0,
    scan_symbols     TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS referral_earnings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    referred_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source      TEXT    NOT NULL,
    amount_usdt REAL    NOT NULL,
    created_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan           TEXT    NOT NULL,
    amount_usdt    REAL    NOT NULL,
    status         TEXT    NOT NULL DEFAULT 'pending',
    payment_method TEXT    NOT NULL DEFAULT '',
    payment_id     TEXT    NOT NULL DEFAULT '',
    created_at     REAL    NOT NULL,
    expires_at     REAL    NOT NULL DEFAULT 0,
    paid_at        REAL
);

CREATE INDEX IF NOT EXISTS idx_users_telegram   ON users(telegram_id);
CREATE INDEX IF NOT EXISTS idx_users_referral   ON users(referral_code);
CREATE INDEX IF NOT EXISTS idx_subs_user        ON subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_referral_referrer ON referral_earnings(referrer_id);
CREATE INDEX IF NOT EXISTS idx_invoices_user    ON invoices(user_id);
CREATE INDEX IF NOT EXISTS idx_invoices_status  ON invoices(status);
"""


class Database:
    """Thin wrapper over a SQLite connection with schema bootstrap.

    Usage::

        db = Database("data/saas.db")
        db.init()
        with db.connect() as conn:
            conn.execute("SELECT ...")
    """

    def __init__(self, path: str = DEFAULT_DB_PATH) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection with foreign keys ON; commit on clean exit."""
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row  # access columns by name
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        """Create all tables and indexes if they don't exist (idempotent)."""
        with self.connect() as conn:
            conn.executescript(_SCHEMA)
