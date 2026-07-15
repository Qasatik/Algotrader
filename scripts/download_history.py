#!/usr/bin/env python3
"""D1 — Historical data pipeline: bulk-download Bybit klines to Parquet.

Downloads 1m (or any interval) candles for a symbol and stores them as a
columnar Parquet file for fast backtests/training. Bybit returns max 1000
candles per request, so we page backward from "now".

Usage:
    python scripts/download_history.py --symbol BTCUSDT --interval 1 --days 365
    python scripts/download_history.py --symbol ETHUSDT --interval 5 --days 180
"""
from __future__ import annotations

import argparse
import asyncio
import os
import time

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from core.exchange import BybitExchange
from utils.logger import get_logger

log = get_logger("download")

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")


async def _fetch_all(exchange: BybitExchange, symbol: str, interval: str,
                     days: int) -> pd.DataFrame:
    """Page through kline history until we cover `days` back from now."""
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 86_400_000
    # request 1000 candles at a time
    batch = 1000

    all_rows: list[list[str]] = []
    cursor = now_ms
    total = 0

    while cursor > start_ms:
        rows = await asyncio.to_thread(exchange.get_klines, symbol, interval, batch)
        if not rows:
            break
        # Bybit returns newest-first
        oldest = int(rows[-1][0])
        all_rows.extend(rows)
        total += len(rows)
        log.info("fetched", symbol=symbol, batch=len(rows), total=total,
                 oldest=_ms_to_date(oldest))
        if oldest <= start_ms:
            break
        cursor = oldest - 1
        await asyncio.sleep(0.15)  # be polite to the API

    df = _rows_to_df(all_rows)
    df = df[df["open_time"] >= pd.Timestamp(start_ms, unit="ms")]
    return df.drop_duplicates(subset="open_time").sort_values("open_time").reset_index(drop=True)


def _rows_to_df(rows: list[list[str]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["start", "open", "high", "low", "close",
                                     "volume", "turnover"])
    for c in ("open", "high", "low", "close", "volume", "turnover"):
        df[c] = pd.to_numeric(df[c])
    df["start"] = pd.to_numeric(df["start"])
    df["open_time"] = pd.to_datetime(df["start"], unit="ms", utc=True)
    return df


def _interval_to_ms(interval: str) -> int:
    table = {"1": 60_000, "3": 180_000, "5": 300_000, "15": 900_000,
             "30": 1_800_000, "60": 3_600_000, "240": 14_400_000}
    return table.get(str(interval), 60_000)


def _ms_to_date(ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


def save_parquet(df: pd.DataFrame, symbol: str, interval: str) -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{symbol}_{interval}m.parquet")
    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(table, path)
    return path


async def main() -> None:
    p = argparse.ArgumentParser(description="Download Bybit kline history to Parquet")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--interval", default="1", help="candle interval (1,5,15,60,240)")
    p.add_argument("--days", type=int, default=365)
    args = p.parse_args()

    log.info("start_download", symbol=args.symbol, interval=args.interval, days=args.days)
    exchange = BybitExchange()
    df = await _fetch_all(exchange, args.symbol, args.interval, args.days)
    path = save_parquet(df, args.symbol, args.interval)
    log.info("done", rows=len(df), path=path,
             start=str(df["open_time"].iloc[0]), end=str(df["open_time"].iloc[-1]))
    print(f"\nSaved {len(df):,} candles -> {path}")


if __name__ == "__main__":
    asyncio.run(main())
