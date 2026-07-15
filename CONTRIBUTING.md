# Contributing

Thanks for contributing! This project follows a standard fork → branch → PR flow.

## Development setup

```bash
git clone <repo-url> && cd bybit-algo-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install ruff pytest pytest-asyncio pre-commit
pre-commit install
cp .env.example .env   # fill in testnet values
```

## Workflow

1. Create a branch: `git checkout -b feat/short-description`.
2. Make changes. Keep commits focused; follow [Conventional Commits](https://www.conventionalcommits.org/).
3. Run checks locally:
   ```bash
   make lint test
   ```
4. Push and open a PR against `main`. CI must pass (ruff + pytest + Docker build).
5. `production` deploys require manual approval.

## Code style

- Python 3.10+, type hints encouraged, line length 100.
- `ruff` enforces style; `black` formatting is configured in `pyproject.toml`.
- New logic in `core/`, `ml/`, `bot/` must have unit tests in `tests/`.
- Never commit secrets or `.env`. Never disable risk circuit breakers in PRs.

## Commit message format

```
<type>(<scope>): <subject>

feat(strategy): add ATR trailing stop
fix(risk): correct daily drawdown reset at UTC midnight
docs(readme): document maker-order fallback
```
