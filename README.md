# 🤖 Bybit Algo Trading Bot (GPU + ML + Telegram Admin + 2FA)

A production-grade, event-driven algorithmic trading bot for **Bybit (V5 unified account)** that runs **GPU-based ML inference** for short-horizon price prediction, ships with a **Telegram admin panel protected by TOTP 2FA**, full **CI/CD**, Docker GPU support, and Prometheus/Grafana observability.

---

## 🌐 1. Hosting: closest to Bybit for minimum latency

**Why it matters:** Bybit's matching engine and REST/WebSocket API are hosted on **AWS in Singapore (`ap-southeast-1`)**. The single biggest latency win is running the bot in the **same AWS region / same data center**. Cross-region or cross-cloud adds 20–150 ms — enough to lose fills and get slipped.

### ✅ Recommended: AWS `ap-southeast-1` (Singapore)

| Goal | Instance | GPU | VRAM | Notes |
|------|----------|-----|------|-------|
| Cheapest GPU, same region | `g4dn.xlarge` | 1× T4 | 16 GB | Fine for small model inference |
| Balanced (recommended) | `g5.2xlarge` | 1× A10G | 24 GB | Good price/perf |
| **~50 GB VRAM target** | `g5.48xlarge` | 8× A10G | 8×24=192 GB | Use 1 GPU or pool |
| High-end | `p4d.24xlarge` | 8× A100 | 8×40=320 GB | For large models |
| Single big GPU | `p4de.24xlarge` | 8× A100 | 8×80=640 GB | |

> **About "50 GB GPU":** there is no single consumer GPU with exactly 50 GB. The closest single-GPU options are **NVIDIA L40S (48 GB)**, **RTX A6000 (48 GB)**, or **A100 (40/80 GB)**. On AWS Singapore, request an **L40S** (`g6e`) or use an **A100** instance. If you only need inference, a single 24 GB A10G is more than enough for this bot's GRU model.

### Alternative providers with Singapore presence (single ~48 GB GPU)

| Provider | Region | GPU option | Latency to Bybit |
|----------|--------|-----------|------------------|
| **Vultr Cloud GPU** | Singapore | A100 80 GB | ~2–6 ms |
| **RunPod / Vast.ai** | SG | RTX A6000 48 GB | ~2–8 ms |
| **Lambda Labs** | SG | H100 / A100 | ~2–6 ms |
| **Tencent / Alibaba Cloud** | Singapore | various | ~3–10 ms |

### Verify your latency (do this before trusting a host)

```bash
python scripts/latency_check.py
```
Target: **median (p50) < 5 ms** to `api.bybit.com`. If you see >20 ms, you are in the wrong region.

---

## 🏗️ 2. Architecture

```
                 ┌──────────────────────────────────────────────┐
   Bybit WS  ──▶ │ MarketDataFeed (orderbook + klines, in-mem)   │
                 └───────────────┬──────────────────────────────┘
                                 │ closed candle
                                 ▼
                 ┌──────────────────────────────────────────────┐
                 │ Strategy = ML(InferenceEngine on GPU)         │
                 │          + order-book imbalance confirmation  │
                 └───────────────┬──────────────────────────────┘
                                 │ Signal (BUY/SELL/HOLD)
                                 ▼
                 ┌──────────────────────────────────────────────┐
                 │ RiskManager (position size, exposure limits)  │
                 └───────────────┬──────────────────────────────┘
                                 │ ApprovedTrade
                                 ▼
                 ┌──────────────────────────────────────────────┐
                 │ OrderManager (market + SL/TP bracket)         │
                 └──────────────────────────────────────────────┘

   Control plane:  Telegram bot ──2FA(TOTP)──▶ TradingEngine.start/stop/kill
   Observability:  Prometheus (:9090/metrics) + Grafana (:3000)
```

**Data flow is event-driven:** the bot reacts to each *closed* candle, runs GPU inference, and only places orders that pass risk checks. The Telegram bot can start/stop/flatten the engine at any time.

---

## 📁 3. Project structure

```
bybit-algo-bot/
├── main.py                  # Entry point (asyncio loop)
├── config/settings.py       # Pydantic-validated config from .env
├── core/
│   ├── exchange.py          # Bybit V5 HTTP wrapper + retry + latency
│   ├── data_feed.py         # WebSocket orderbook + klines
│   ├── strategy.py          # ML + orderbook strategy
│   ├── risk_manager.py      # Position sizing & exposure gate
│   ├── order_manager.py     # Order execution (SL/TP brackets)
│   └── engine.py            # Orchestrator (start/stop/kill_switch)
├── ml/
│   ├── features.py          # RSI, MACD, Bollinger, vol features
│   ├── model.py             # PyTorch GRU classifier
│   └── inference.py         # GPU inference engine
├── security/totp.py         # RFC 6238 TOTP (2FA), stdlib only
├── bot/telegram_admin.py    # Telegram admin panel + 2FA sessions
├── utils/{logger,metrics}.py
├── tests/                   # pytest: 2FA, features, risk
├── scripts/{latency_check,bootstrap_model}.py
├── deploy/{deploy.sh,prometheus.yml}
├── Dockerfile               # CUDA 12.1 GPU image
├── docker-compose.yml       # bot + prometheus + grafana
└── .github/workflows/       # ci.yml + deploy.yml
```

---

## 🚀 4. Quick start (local, testnet)

```bash
cd bybit-algo-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. Configure secrets
cp .env.example .env          # then edit (see §5)

# 2. Create a model checkpoint (dummy, replace later)
python scripts/bootstrap_model.py

# 3. Verify latency
python scripts/latency_check.py

# 4. Run
python main.py
```

Then talk to your Telegram bot: `/start` → `/setup_2fa` (scan QR) → `/auth <code>` → `/status`.

---

## 🔐 5. Configuration (`.env`)

| Variable | Description |
|----------|-------------|
| `BYBIT_API_KEY` / `BYBIT_API_SECRET` | Bybit API keys (testnet first!) |
| `BYBIT_TESTNET` | `true` = paper/testnet, `false` = live |
| `TRADING_SYMBOL` | Linear perp, e.g. `BTCUSDT` |
| `RISK_PER_TRADE` | Fraction of equity risked per trade (0.01 = 1%) |
| `LEVERAGE` | Position leverage (x) |
| `ML_DEVICE` | `cuda:0` or `cpu` |
| `ML_MODEL_PATH` | PyTorch checkpoint path |
| `ML_CONFIDENCE_THRESHOLD` | Min softmax confidence to act |
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `TELEGRAM_ADMIN_IDS` | Comma-separated allowed Telegram user IDs |
| `TOTP_SECRET` | Base32 2FA secret (generate below) |
| `SESSION_TTL` | Seconds a 2FA unlock lasts |

### Generate a 2FA secret

```bash
python -m security.totp --generate
# -> Secret: XXXX...   add it to .env as TOTP_SECRET
```
Then in Telegram: `/setup_2fa` → scan the QR with Google Authenticator / Authy / 1Password.

---

## 📱 6. Telegram admin commands

| Command | 2FA? | Action |
|---------|------|--------|
| `/start`, `/help` | — | Info |
| `/setup_2fa` | — | Generate QR code |
| `/auth <code>` | — | Unlock privileged commands |
| `/status` | read | Engine state, equity, stats |
| `/positions` | read | Open positions |
| `/pnl` | read | Unrealized PnL |
| `/start_engine` | ✅ | Start trading |
| `/stop_engine` | ✅ | Graceful stop (keeps positions) |
| `/pause` / `/resume` | ✅ | Toggle order placement |
| `/kill` | ✅ | **EMERGENCY**: stop + flatten position |

**Security model:** user-ID whitelist + TOTP 2FA session (TTL-based). Read-only commands are always available; anything that moves money requires an active 2FA session.

---

## 🐳 7. Docker (GPU) deployment

Requires **NVIDIA Container Toolkit** on the host (`nvidia-smi` should work).

```bash
cp .env.example .env && nano .env       # fill secrets
docker compose up -d --build            # bot + prometheus + grafana
docker compose logs -f bot
```

- Metrics: `http://<host>:9090/metrics`
- Grafana: `http://<host>:3000` (admin / `$GRAFANA_PASSWORD`)

---

## 🔁 8. CI/CD (GitHub Actions)

- **`.github/workflows/ci.yml`** — on every push/PR: `ruff` lint, compile check, pytest, Docker build (no push).
- **`.github/workflows/deploy.yml`** — on `main`: build + push image to **GHCR**, then SSH-deploy to the Singapore host (`docker compose pull && up -d`).

### Required GitHub secrets (Settings → Secrets)

| Secret | Purpose |
|--------|---------|
| `DEPLOY_HOST` | Singapore server IP/hostname |
| `DEPLOY_USER` | SSH user |
| `DEPLOY_SSH_KEY` | Private SSH key |
| `DEPLOY_PATH` | Project path on server (e.g. `/opt/bybit-bot`) |

Set the `production` environment to **require manual approval** for safe releases.

---

## 🧪 9. Tests

```bash
pytest -v
```
Covers: TOTP round-trips + RFC 4226 vectors, feature shapes/normalization, risk-manager sizing & exposure limits.

---

## ⚠️ 10. Risk disclaimer

Algorithmic trading of leveraged crypto products carries **substantial risk of total capital loss**. This software is provided for educational purposes with **no warranty**. Always:
1. Validate on **testnet** first.
2. Start with the **smallest possible size**.
3. Never trade money you cannot afford to lose.
4. Audit the model, risk parameters, and order logic before going live.

The included model is **untrained** (`bootstrap_model.py`). You must supply a properly trained and backtested checkpoint before live trading.
