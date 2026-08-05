#!/usr/bin/env python3
"""One-shot algo-trader status: is it running? what's the balance? is it alive?

Self-contained cheat-sheet script. Run it to get an instant answer without
poking around logs / processes / exchange by hand each time.

Usage:
    PYTHONPATH=. python3 scripts/bot_status.py
    PYTHONPATH=. python3 scripts/bot_status.py --json     # machine-readable

What it reports
---------------
1. PROCESS  — is the carry runner alive? pid / uptime / full command
2. ACTIVITY — newest mtime across the bot's log files ("last heartbeat")
3. BALANCE  — unified-account total equity (USDT) + per-coin + open position
4. VERDICT  — RUNNING+ACTIVE / RUNNING-STALE / STOPPED

Exit code: 0 when the bot is running & active, non-zero otherwise (handy for
cron / monitoring).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow running without PYTHONPATH=. (find project root = parent of scripts/)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# --- tunables -------------------------------------------------------------
RUNNER_PATTERN = "run_carry"              # matches run_carry_testnet.py AND run_carry_multi.py
SERVICE_NAME = "carry-bot"                # systemd --user unit that runs the bot
LOG_FILES = [
    "logs/carry.log",
    "data/carry_multi.log",
    "logs/bot_stdout.log",
    "data/saas_bot.log",
]
# The runner prints a heartbeat to stdout EVERY poll (~10s). Under systemd that
# lands in the journal, NOT in a log file — so the journal is the source of
# truth for "is it alive". File mtimes are only a fallback.
ACTIVE_THRESHOLD_SEC = 900  # heartbeat within 15 min => bot is "active"
DEFAULT_SYMBOL = os.environ.get("TRADING_SYMBOL", "BTCUSDT")


# --------------------------------------------------------------------------- #
# 1. PROCESS
# --------------------------------------------------------------------------- #
def find_process(pattern: str) -> dict | None:
    """Return {pid, etimes_sec, cmd} for the first matching process, or None."""
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid=,etimes=,args="], text=True, stderr=subprocess.DEVNULL
        )
    except Exception:
        return None
    for line in out.splitlines():
        if pattern in line and "bot_status" not in line:
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            try:
                pid = int(parts[0])
                etimes = int(parts[1])
            except ValueError:
                continue
            return {"pid": pid, "etimes_sec": etimes, "cmd": parts[2].strip()}
    return None


def fmt_uptime(sec: int) -> str:
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


# --------------------------------------------------------------------------- #
# 2. ACTIVITY (log freshness)
# --------------------------------------------------------------------------- #
def newest_log(root: Path, files: list[str]) -> dict:
    """Find the most recently modified log file under `root`."""
    best: dict = {"path": None, "age_sec": None, "mtime": None}
    for rel in files:
        p = root / rel
        if not p.exists():
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        age = time.time() - mtime
        if best["age_sec"] is None or age < best["age_sec"]:
            best = {"path": rel, "age_sec": age, "mtime": mtime}
    return best


def fmt_age(sec: float | None) -> str:
    if sec is None:
        return "no logs found"
    if sec < 60:
        return f"{int(sec)}s ago"
    if sec < 3600:
        return f"{int(sec // 60)}m ago"
    if sec < 86400:
        return f"{int(sec // 3600)}h {int((sec % 3600) // 60)}m ago"
    return f"{int(sec // 86400)}d ago"


# --------------------------------------------------------------------------- #
# 2b. SYSTEMD SERVICE + JOURNAL (true heartbeat source)
# --------------------------------------------------------------------------- #
def service_state(unit: str) -> str | None:
    """Return systemd --user sub-state (e.g. 'running', 'failed') or None."""
    try:
        out = subprocess.check_output(
            ["systemctl", "--user", "show", unit, "--property=SubState", "--value"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        return out or None
    except Exception:
        return None


def journal_last_age(unit: str) -> dict:
    """Age of the most recent journal line for `unit`.

    The runner prints a heartbeat every poll, so the newest journal entry is the
    freshest proof the loop is alive. Uses ``-o json`` so we get
    ``__REALTIME_TIMESTAMP`` (UTC epoch microseconds) — no local-time parsing.
    Returns {age_sec, line, source:'journal'}.
    """
    try:
        out = subprocess.check_output(
            ["journalctl", "--user", "-u", unit, "-n", "1", "-o", "json", "--no-pager"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return {"age_sec": None, "line": None, "source": "journal"}
    if not out:
        return {"age_sec": None, "line": None, "source": "journal"}
    age = None
    line = out
    try:
        obj = json.loads(out)
        ts_us = int(obj.get("__REALTIME_TIMESTAMP", 0) or 0)
        if ts_us:
            age = time.time() - ts_us / 1_000_000.0
        msg = obj.get("MESSAGE")
        if msg:
            line = msg
    except (ValueError, TypeError):
        pass
    return {"age_sec": age, "line": line, "source": "journal"}


def detect_activity(root: Path, unit: str, files: list[str]) -> dict:
    """Pick the freshest activity signal: journal first, log files as fallback."""
    j = journal_last_age(unit)
    if j["age_sec"] is not None:
        return {**j, "path": f"journal:{unit}"}
    log = newest_log(root, files)
    return {**log, "line": None, "source": "file"}


# --------------------------------------------------------------------------- #
# 3. BALANCE (live exchange query)
# --------------------------------------------------------------------------- #
def query_balance(mainnet: bool, symbol: str) -> dict:
    """Best-effort live balance + position snapshot from Bybit."""
    info: dict = {"ok": False, "error": None}
    try:
        from core.exchange import BybitExchange  # local import: heavy deps
    except Exception as exc:
        info["error"] = f"import failed: {exc}"
        return info

    ex = None
    try:
        ex = BybitExchange(testnet=False if mainnet else True)
        total, coins = ex.get_total_equity()
        info["total_equity_usdt"] = round(total, 4)
        info["coins"] = [
            {
                "coin": c["coin"],
                "wallet": round(float(c["wallet_balance"]), 6),
                "usd": round(float(c["usd_value"]), 4),
            }
            for c in coins
            if float(c.get("usd_value", 0) or 0) > 0.0001
            or float(c.get("wallet_balance", 0) or 0) > 0.0001
        ]
        # open perp position size for the trading symbol
        try:
            positions = ex.get_positions(symbol)
            perp = next((p for p in positions if p.get("symbol") == symbol), None)
            size = abs(float(perp.get("size", 0))) if perp else 0.0
            side = perp.get("side", "") if perp else ""
            upnl = float(perp.get("unrealisedPnl", 0)) if perp else 0.0
            info["position"] = {
                "symbol": symbol,
                "side": side,
                "size_btc": round(size, 6),
                "upnl_usdt": round(upnl, 4),
            }
        except Exception as exc:
            info["position"] = {"error": str(exc)}
        info["ok"] = True
    except Exception as exc:
        info["error"] = str(exc)
    finally:
        if ex is not None:
            try:
                ex.close()
            except Exception:
                pass
    return info


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Algo-trader status at a glance.")
    ap.add_argument("--symbol", default=DEFAULT_SYMBOL, help="Trading symbol (default BTCUSDT)")
    ap.add_argument("--testnet", action="store_true", help="Query testnet balance (default: mainnet)")
    ap.add_argument("--json", action="store_true", help="Emit JSON only.")
    args = ap.parse_args()

    root = _PROJECT_ROOT
    proc = find_process(RUNNER_PATTERN)
    svc = service_state(SERVICE_NAME)  # e.g. 'running' / 'failed' / None
    act_info = detect_activity(root, SERVICE_NAME, LOG_FILES)

    # mainnet vs testnet: infer from the running command's --mainnet flag
    mainnet = (not args.testnet)
    if proc and "--mainnet" in proc["cmd"]:
        mainnet = True
    elif proc and "--testnet" in proc["cmd"]:
        mainnet = False

    bal = query_balance(mainnet=mainnet, symbol=args.symbol)

    running = bool(proc) or (svc == "running")
    active = bool(act_info["age_sec"] is not None
                  and act_info["age_sec"] <= ACTIVE_THRESHOLD_SEC)
    if running and active:
        verdict = "RUNNING + ACTIVE"
        exit_code = 0
    elif running:
        verdict = "RUNNING but logs look STALE"
        exit_code = 2
    else:
        verdict = "STOPPED"
        exit_code = 1

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if args.json:
        print(json.dumps({
            "verdict": verdict,
            "running": running,
            "active": active,
            "mainnet": mainnet,
            "process": proc,
            "service": svc,
            "activity": {**act_info, "age_human": fmt_age(act_info["age_sec"])},
            "balance": bal,
            "checked_at_utc": now,
        }, indent=2))
        return exit_code

    mode = "MAINNET" if mainnet else "TESTNET"
    print(f"\n{'═' * 64}")
    print(f"  🤖 ALGO-TRADER STATUS  |  {mode}  |  {now}")
    print(f"{'═' * 64}")

    # --- process ---
    if proc:
        print(f"\n  ▶ PROCESS:  \033[32mRUNNING\033[0m  (pid {proc['pid']}, up {fmt_uptime(proc['etimes_sec'])})")
        print(f"             {proc['cmd']}")
    else:
        print(f"\n  ▶ PROCESS:  \033[31mNOT RUNNING\033[0m  (no '{RUNNER_PATTERN}' process found)")

    # --- service ---
    if svc:
        scolor = "\033[32m" if svc == "running" else "\033[31m"
        print(f"\n  ⚙ SERVICE:   {SERVICE_NAME}.service  {scolor}{svc}\033[0m  (systemd --user)")
    else:
        print(f"\n  ⚙ SERVICE:   {SERVICE_NAME}.service  \033[33mnot found\033[0m  (not a systemd unit)")

    # --- activity (journal heartbeat preferred over log-file mtime) ---
    if act_info["age_sec"] is not None:
        color = "\033[32m" if active else "\033[33m"
        src = act_info.get("source", "?")
        print(f"\n  ❤ ACTIVITY: last heartbeat {color}{fmt_age(act_info['age_sec'])}\033[0m  "
              f"(via {src}: {act_info.get('path')})")
        if act_info.get("line"):
            print(f"             \u201c{act_info['line'][:90]}\u201d")
    else:
        print("\n  ❤ ACTIVITY: no journal entries and no log files found")

    # --- balance ---
    if bal.get("ok"):
        eq = bal.get("total_equity_usdt", 0.0)
        print(f"\n  💰 BALANCE:  total equity \033[1m{eq:,.4f} USDT\033[0m")
        for c in bal.get("coins", []):
            print(f"             • {c['coin']:<6} wallet={c['wallet']:<14} ≈ {c['usd']:,.4f} USD")
        pos = bal.get("position", {})
        if isinstance(pos, dict) and "size_btc" in pos and pos["size_btc"] > 0:
            pnl = pos.get("upnl_usdt", 0.0)
            pcol = "\033[32m" if pnl >= 0 else "\033[31m"
            print(f"             • position {pos.get('symbol')} {pos.get('side')} "
                  f"{pos['size_btc']} BTC  uPnL {pcol}{pnl:+.4f} USDT\033[0m")
        else:
            print("             • no open position (FLAT)")
    else:
        print(f"\n  💰 BALANCE:  \033[31mquery failed\033[0m — {bal.get('error')}")

    # --- verdict ---
    vcolor = "\033[32m" if exit_code == 0 else "\033[31m"
    print(f"\n  {vcolor}✅ VERDICT: {verdict}\033[0m")
    print(f"{'═' * 64}\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
