# 📊 Bybit Delta-Neutral Carry Bot

> **Production-grade funding-rate arbitrage bot running live on Bybit mainnet.**
> Earns funding yield by holding a delta-neutral position (short perpetual + long spot)
> across multiple symbols simultaneously, with full risk management and observability.

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Bybit V5](https://img.shields.io/badge/Bybit-V5%20API-orange.svg)](https://bybit-exchange.github.io/docs/v5/intro)

> 💡 **Prefer a managed service?** A hosted version with Telegram control,
>> multi-symbol automation, and zero setup is available as a subscription.
>> The engine you see here is 100% open-source — run it yourself or let us host it.

---

## 🎯 What it does

The bot executes a **delta-neutral funding carry** strategy:

1. **Short** a perpetual futures contract (e.g. BTCUSDT)
2. **Long** the equivalent spot pair (e.g. BTCUSDT spot)
3. Price moves cancel out → **collect funding payments every 8 hours**
4. Close when funding turns negative (EV-gated) or basis blows out (squeeze guard)

```
  FLAT  ──funding≥min──▶  HEDGED  ──basis blowout──▶  FLAT
                               │
                               ├─drift>threshold─▶ REBALANCE
                               └─collects funding every 8h
```

### Why it works

When perpetual funding is **positive**, longs pay shorts. By being short perp + long spot
(delta = 0), we're market-neutral but **receive the funding payment** as pure yield.
Typical rates: **6–15% annualised** on major crypto pairs.

---

## ✨ Key features

| Feature | Description |
|---------|-------------|
| **Multi-symbol trading** | Run N carry positions simultaneously across BTC, ETH, BNB, SOL, etc. with auto-detected lot sizes |
| **Funding rate scanner** | Real-time scanner ranking 14+ symbols by funding yield with annualised rate |
| **EV-gated exit** | Only closes when projected holding loss > round-trip close cost (prevents churning) |
| **Basis guard** | Flattens instantly if perp premium over spot exceeds threshold (short-squeeze protection) |
| **Exchange-side stop-loss** | Server-side SL on Bybit — protects position even when bot is offline |
| **Position reconciliation** | Syncs with live exchange state on restart — never opens duplicate positions |
| **Auto-rebalance** | Re-aligns perp/spot legs when delta drifts beyond threshold |
| **Liquidation monitor** | Warns when mark price approaches liquidation |
| **Conviction sizing** | Scales position size with entry confidence (funding cushion × basis safety) |
| **Telegram push** | Real-time notifications on open/close/rebalance/errors |
| **File logging** | Rotating log files (5 MB × 5) survive terminal close |
| **systemd ready** | Runs as a daemon service with `--yes` flag for automation |

---

## 🏗️ Architecture

```
  ┌─────────────────────────────────────────────────────────┐
  │                    CarryStrategy                         │
  │  ┌─────────────┐   ┌──────────────┐   ┌──────────────┐ │
  │  │  decide()   │──▶│  execute()   │──▶│  _log_trade  │ │
  │  │ (pure logic)│   │ (real orders)│   │   (CSV log)  │ │
  │  └──────┬──────┘   └──────┬───────┘   └──────────────┘ │
  │         │                 │                             │
  │    funding+basis     perp short + spot long             │
  └─────────┼─────────────────┼─────────────────────────────┘
            │                 │
  ┌─────────▼─────────┐ ┌────▼──────────────────┐
  │  BybitExchange    │ │  Risk Management      │
  │  (V5 HTTP + retry)│ │  • Basis guard (50bps)│
  │                   │ │  • EV-gated exit      │
  │  get_funding_rate │ │  • Exchange SL (15%)  │
  │  get_spot_price   │ │  • Liq proximity      │
  │  place_order      │ │  • Rebalance (20bps)  │
  │  set_trading_stop │ │  • Max hold time      │
  └───────────────────┘ └───────────────────────┘
```

**Design principle:** `decide()` is pure logic (reads market data, returns action,
updates state) — fully unit-testable with a mock exchange. `execute()` turns the
action into real orders with rollback safety.

---

## 🚀 Quick start

```bash
cd bybit-algo-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Configure API keys
cp .env.example .env  # edit with your Bybit API key/secret

# 1. Scan for the best funding rates
PYTHONPATH=. python3 scripts/scan_funding.py

# 2. Dry-run (no orders, just decisions)
PYTHONPATH=. python3 scripts/run_carry_multi.py --dry-run \
  --symbols BTCUSDT,ETHUSDT,BNBUSDT --interval 5

# 3. Live on mainnet (REAL MONEY)
PYTHONPATH=. python3 scripts/run_carry_multi.py --mainnet --yes \
  --symbols BTCUSDT,ETHUSDT,BNBUSDT --interval 5 \
  --equity-fraction 0.7 --max-notional 50 --leverage 2
```

### Monitor your positions

```bash
# Live position dashboard (auto-refresh)
PYTHONPATH=. python3 scripts/show_position.py --mainnet --watch 10

# Trade history
PYTHONPATH=. python3 scripts/show_trades.py --mainnet
```

---

## 📡 Funding rate scanner

```bash
PYTHONPATH=. python3 scripts/scan_funding.py
```

```
═══════════════════════════════════════════════════════════════════════
  📡 FUNDING RATE SCANNER  |  14 symbols
═══════════════════════════════════════════════════════════════════════
  Symbol       Funding/8h   Annualised  Basis bps
  BNBUSDT      +0.0100% ★   +11.0%       -3.4
  AVAXUSDT     +0.0094%     +10.3%       -4.5
  BTCUSDT      +0.0058%      +6.3%       -5.3
  ETHUSDT      +0.0029%      +3.1%       -5.3
═══════════════════════════════════════════════════════════════════════
```

---

## 📊 Backtest results

See [`CARRY_RESULTS.md`](CARRY_RESULTS.md) for the full analysis. Headline:

- **Passive carry** (always hedged): **+18.2% annualised** on historical funding data
- **Timed strategy** (enter/exit on threshold): **+12.4% annualised** after fees
- **Round-trip cost:** 0.31% (perp taker 0.055% + spot taker 0.10% + slippage, ×2 legs)

---

## 🛡️ Risk management

| Layer | What it protects against |
|-------|------------------------|
| **Basis guard** (50 bps) | Short squeeze — perp premium blowout |
| **Exchange-side SL** (15%) | Liquidation — server-side, works offline |
| **EV-gated exit** | Churning — only close when it's worth the fee |
| **Rebalance** (20 bps drift) | Delta drift — legs out of alignment |
| **Liq monitor** (15% proximity) | Early warning before liquidation |
| **Rollback safety** | Unhedged short — closes perp if spot leg fails |
| **Max notional cap** | Over-leverage — hard cap per position |

---

## 🧪 Testing

```bash
PYTHONPATH=. python3 -m pytest tests/ -v
```

27 tests covering: entry/exit logic, basis guard, EV-gated exit, conviction sizing,
rebalance, reconciliation, rollback safety, trade logging, and more.

---

## 📁 Project structure

```
bybit-algo-bot/
├── core/
│   ├── carry_strategy.py     # Delta-neutral carry state machine
│   ├── carry_stats.py        # Position statistics
│   ├── exchange.py           # Bybit V5 HTTP wrapper + retry + latency
│   ├── engine.py             # ML trading engine orchestrator
│   ├── strategy.py           # ML + orderbook strategy
│   ├── risk_manager.py       # Position sizing & circuit breakers
│   └── order_manager.py      # Order execution (maker/taker)
├── backtest/
│   ├── carry.py              # Carry strategy backtest
│   ├── engine.py             # Vectorised backtesting engine
│   └── metrics.py            # Sharpe, Sortino, max drawdown
├── ml/
│   ├── model.py              # PyTorch GRU price predictor
│   ├── features.py           # RSI, MACD, Bollinger features
│   ├── dataset.py            # Triple-barrier labeling
│   └── train.py              # Training pipeline
├── scripts/
│   ├── run_carry_multi.py    # Multi-symbol carry runner
│   ├── run_carry_testnet.py  # Single-symbol carry runner
│   ├── scan_funding.py       # Funding rate scanner
│   ├── show_position.py      # Live position dashboard
│   └── show_trades.py        # Trade history viewer
├── bot/telegram_admin.py     # Telegram admin panel + TOTP 2FA
├── utils/
│   ├── logger.py             # structlog + rotating file
│   └── notifier.py           # Telegram push notifications
├── config/settings.py        # Pydantic-validated config
├── tests/                    # 27 pytest tests
├── Dockerfile                # CUDA 12.1 GPU image
└── .github/workflows/        # CI/CD pipelines
```

---

## 🛠️ Tech stack

- **Python 3.12+** — type hints, dataclasses, `match` statements
- **pybit** — official Bybit V5 SDK
- **structlog** — structured JSON logging
- **Pydantic** — validated configuration
- **tenacity** — exponential-backoff retries
- **pytest** — unit testing
- **ruff + black** — linting & formatting
- **Docker + systemd** — deployment

---

## 📜 License

MIT — see [LICENSE](LICENSE).

---

## ⚠️ Disclaimer

This software is for educational purposes. Cryptocurrency trading carries significant
risk. The authors are not responsible for any financial losses. Always test on testnet
first and never trade with money you can't afford to lose.
