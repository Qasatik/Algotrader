"""Net-worth & P&L tracking for the carry bot — measured in USDT *and* BTC.

The carry strategy earns USDT funding.  For an "accumulate BTC" goal the
honest north-star metric is **account net worth expressed in BTC**
(``equity_usdt / btc_price``): it rises only when USDT equity outpaces
BTC's price appreciation.  Snapshots are appended to a CSV so a trend and
annualised yield (APR) can be computed over time.

Typical use (see :mod:`scripts.show_pnl`)::

    snap = pnl_tracker.snapshot(exchange)      # one mark-to-market reading
    pnl_tracker.append_history(path, snap)     # persist it
    s = pnl_tracker.summary(pnl_tracker.load_history(path))  # P&L since baseline
"""
from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.exchange import BybitExchange

DEFAULT_PNL_LOG = "data/carry_pnl.csv"
FIELDS = ["timestamp", "equity_usdt", "btc_price", "equity_btc"]


@dataclass
class NetWorth:
    """A single mark-to-market net-worth reading."""

    timestamp: str  # ISO-8601 UTC
    equity_usdt: float  # total account equity in USDT
    btc_price: float  # BTC/USDT spot at snapshot time
    equity_btc: float  # equity_usdt / btc_price — the "accumulate BTC" metric


def snapshot(exchange: BybitExchange, btc_symbol: str = "BTCUSDT") -> NetWorth | None:
    """Take a mark-to-market net-worth snapshot (USDT + BTC-denominated).

    Returns ``None`` if account equity cannot be read (e.g. not connected).
    """
    equity_usdt, _coins = exchange.get_total_equity()
    if equity_usdt <= 0.0:
        return None
    btc_price = exchange.get_spot_price(btc_symbol) or 0.0
    equity_btc = equity_usdt / btc_price if btc_price > 0 else 0.0
    return NetWorth(
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        equity_usdt=round(equity_usdt, 8),
        btc_price=round(btc_price, 2),
        equity_btc=round(equity_btc, 8),
    )


def append_history(path: str, snap: NetWorth) -> None:
    """Append a snapshot to the CSV, creating the header + dirs if needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_header = not p.exists()
    with open(p, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            w.writeheader()
        w.writerow(asdict(snap))


def load_history(path: str = DEFAULT_PNL_LOG) -> list[NetWorth]:
    """Read all snapshots (oldest first). Malformed rows are skipped."""
    p = Path(path)
    if not p.exists():
        return []
    out: list[NetWorth] = []
    with open(p, newline="") as f:
        for row in csv.DictReader(f):
            try:
                out.append(NetWorth(
                    timestamp=row["timestamp"],
                    equity_usdt=float(row["equity_usdt"]),
                    btc_price=float(row["btc_price"]),
                    equity_btc=float(row["equity_btc"]),
                ))
            except (KeyError, ValueError):
                continue
    return out


def reset_baseline(path: str) -> NetWorth | None:
    """Truncate history to only the latest snapshot (start a new baseline)."""
    hist = load_history(path)
    if not hist:
        return None
    last = hist[-1]
    p = Path(path)
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerow(asdict(last))
    return last


def _years_between(first_ts: str, last_ts: str) -> float:
    """Elapsed years between two ISO timestamps (>= 1e-9 to avoid div-by-zero)."""
    try:
        t0 = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        return max((t1 - t0).total_seconds() / (365.25 * 86400), 1e-9)
    except ValueError:
        return 0.0


def summary(history: list[NetWorth]) -> dict:
    """Compute P&L since the first snapshot: USDT & BTC delta, % return, APR.

    Returns a minimal ``{"n": ...}`` dict when fewer than 2 snapshots exist.
    """
    if len(history) < 2:
        return {"n": len(history)}
    first, last = history[0], history[-1]
    years = _years_between(first.timestamp, last.timestamp)
    delta_usdt = last.equity_usdt - first.equity_usdt
    delta_btc = last.equity_btc - first.equity_btc
    pct_usdt = delta_usdt / first.equity_usdt * 100.0 if first.equity_usdt else 0.0
    pct_btc = delta_btc / first.equity_btc * 100.0 if first.equity_btc else 0.0
    return {
        "n": len(history),
        "first": first,
        "last": last,
        "years": years,
        "delta_usdt": delta_usdt,
        "delta_btc": delta_btc,
        "pct_usdt": pct_usdt,
        "pct_btc": pct_btc,
        "apr_usdt": pct_usdt / years if years > 0 else 0.0,
        "apr_btc": pct_btc / years if years > 0 else 0.0,
    }
