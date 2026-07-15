# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [1.0.0] - 2026-07-15
### Added
- Event-driven trading engine for Bybit V5 (linear perpetuals).
- GPU ML inference (PyTorch GRU) for short-horizon price-direction prediction.
- Order-book imbalance confirmation in the strategy.
- Risk manager with position sizing, leverage caps, and exposure limits.
- **Circuit breakers**: daily drawdown limit + consecutive-loss cooldown.
- **Maker (limit) order execution** with taker fallback to earn Bybit rebates.
- **ATR-based adaptive stops** (volatility-scaled).
- **Telegram admin bot** with TOTP 2FA gating (start/stop/pause/kill/status/PnL).
- **Telegram event alerts** (fills, failures, halts, kill-switch).
- Historical data pipeline (klines → Parquet).
- Training pipeline with triple-barrier labels + class weighting.
- Vectorized backtesting engine + metrics (Sharpe, Sortino, max-DD, profit factor).
- Prometheus metrics endpoint + Grafana in docker-compose.
- Docker (CUDA 12.1 GPU) image + healthcheck.
- CI/CD: GitHub Actions (lint, test, build, GHCR push, SSH deploy).
- Enterprise docs: SECURITY, CONTRIBUTING, CHANGELOG, LICENSE.

### Security
- `.env` gitignored; secrets via environment only.
- 2FA required for all fund-moving Telegram commands.
