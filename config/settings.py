"""Centralized, validated configuration loaded from environment variables."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load .env if present (local dev). On servers, inject env vars directly.
load_dotenv()


class Settings(BaseSettings):
    """Strongly-typed configuration for the entire bot.

    All values default to safe/testnet-friendly settings so the bot never
    accidentally trades real funds without explicit configuration.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Bybit API -------------------------------------------------
    bybit_api_key: str = Field(default="", alias="BYBIT_API_KEY")
    bybit_api_secret: str = Field(default="", alias="BYBIT_API_SECRET")
    bybit_testnet: bool = Field(default=True, alias="BYBIT_TESTNET")

    # ---- Trading ---------------------------------------------------
    trading_symbol: str = Field(default="BTCUSDT", alias="TRADING_SYMBOL")
    risk_per_trade: float = Field(default=0.01, ge=0.0, le=1.0, alias="RISK_PER_TRADE")
    max_open_positions: int = Field(default=3, ge=1, alias="MAX_OPEN_POSITIONS")
    leverage: int = Field(default=5, ge=1, le=100, alias="LEVERAGE")

    # ---- Risk circuit breakers (R1/R2) ----------------------------
    max_daily_drawdown: float = Field(default=0.03, ge=0.0, le=1.0, alias="MAX_DAILY_DRAWDOWN")
    max_consecutive_losses: int = Field(default=5, ge=1, alias="MAX_CONSECUTIVE_LOSSES")
    cooldown_minutes: int = Field(default=60, ge=0, alias="COOLDOWN_MINUTES")

    # ---- ML / GPU --------------------------------------------------
    ml_device: str = Field(default="cuda:0", alias="ML_DEVICE")
    ml_model_path: str = Field(default="models/price_predictor.pt", alias="ML_MODEL_PATH")
    ml_sequence_len: int = Field(default=128, ge=10, alias="ML_SEQUENCE_LEN")
    ml_confidence_threshold: float = Field(
        default=0.6, ge=0.0, le=1.0, alias="ML_CONFIDENCE_THRESHOLD"
    )

    # ---- Data feed -------------------------------------------------
    orderbook_depth: int = Field(default=50, alias="ORDERBOOK_DEPTH")
    kline_interval: str = Field(default="1", alias="KLINE_INTERVAL")

    # ---- Infrastructure -------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO", alias="LOG_LEVEL"
    )
    http_timeout: int = Field(default=10, ge=1, alias="HTTP_TIMEOUT")
    metrics_port: int = Field(default=9090, ge=1, le=65535, alias="METRICS_PORT")

    # ---- Telegram admin bot ---------------------------------------
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    # Stored as raw string (env-safe); use .admin_ids property for the list.
    telegram_admin_ids: str = Field(default="", alias="TELEGRAM_ADMIN_IDS")
    totp_secret: str = Field(default="", alias="TOTP_SECRET")
    session_ttl: int = Field(default=300, ge=10, alias="SESSION_TTL")

    # ---- Execution ------------------------------------------------
    use_maker_orders: bool = Field(default=True, alias="USE_MAKER_ORDERS")
    maker_reprice_timeout: float = Field(default=2.0, alias="MAKER_REPRICE_TIMEOUT")

    # ---- Derived helpers ------------------------------------------
    @property
    def is_paper_mode(self) -> bool:
        """True when running against the Bybit testnet (no real funds)."""
        return self.bybit_testnet

    @field_validator("trading_symbol")
    @classmethod
    def _normalize_symbol(cls, v: str) -> str:
        return v.strip().upper()

    @property
    def admin_ids(self) -> list[int]:
        """Parse TELEGRAM_ADMIN_IDS into a list of ints.

        Accepts empty, comma-separated ("1,2,3"), or JSON ("[1,2,3]").
        """
        s = self.telegram_admin_ids.strip().strip("[]").replace(" ", "")
        return [int(x) for x in s.split(",") if x]

    def assert_ready_for_live(self) -> None:
        """Guard: refuse to start on mainnet without credentials."""
        if not self.is_paper_mode and not (self.bybit_api_key and self.bybit_api_secret):
            raise RuntimeError(
                "MAINNET trading requested but API key/secret are missing. "
                "Set BYBIT_API_KEY and BYBIT_API_SECRET, or keep BYBIT_TESTNET=true."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()


# Convenience module-level access
settings = get_settings()
