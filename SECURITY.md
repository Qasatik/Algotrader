# 🔒 Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability, **do NOT open a public issue**.
Instead, email the maintainer directly with a description and reproduction steps.
We acknowledge reports within 48 hours and aim for a fix within 7 days.

## Secret Management

- **All secrets live in `.env`**, which is gitignored and **never** committed.
- The included `.env` contains a Telegram bot token and TOTP secret — treat the
  repository as if it may have been exposed and **rotate any leaked credentials**.
- API keys are loaded via environment variables; no secrets are hardcoded.
- CI/CD injects deploy secrets via GitHub Actions encrypted secrets, not the repo.

## 2FA

The Telegram admin panel requires TOTP 2FA (RFC 6238) for any action that moves
funds. Read-only commands are available without 2FA so an operator can always
inspect state. Generate a secret with `python -m security.totp --generate`.

## Trading Risk

This software trades leveraged crypto products and can lose all deployed
capital. There is **no warranty**. Always validate on Bybit **testnet** with
the smallest possible size before any mainnet use.
