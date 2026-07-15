.PHONY: help install dev lint format typecheck test backtest train run docker-up docker-down clean

PYTHON ?= python3

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## Install runtime dependencies
	$(PYTHON) -m pip install -r requirements.txt

dev: ## Install runtime + dev dependencies
	$(PYTHON) -m pip install -r requirements.txt ruff pytest pytest-asyncio pre-commit
	pre-commit install

lint: ## Lint with ruff
	ruff check .

format: ## Format with ruff
	ruff format .
	ruff check --fix .

typecheck: ## Compile-check all modules
	@find . -name '*.py' -not -path './.venv/*' -not -path '*/__pycache__/*' | xargs $(PYTHON) -m py_compile && echo "compile OK"

test: ## Run the test suite
	$(PYTHON) -m pytest -v

backtest: ## Run a backtest (needs data/<symbol>_<interval>m.parquet)
	$(PYTHON) -m backtest.engine --symbol BTCUSDT --interval 1

train: ## Train the model (needs historical data)
	$(PYTHON) -m ml.train --symbol BTCUSDT --interval 1 --epochs 30

run: ## Run the bot locally
	$(PYTHON) main.py

docker-up: ## Build & start the full GPU stack
	docker compose up -d --build

docker-down: ## Stop the stack
	docker compose down

clean: ## Remove caches and artifacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache
