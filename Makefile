PYTHON ?= python3.12
COMPOSE = docker compose -p ai-futures
.PHONY: setup test lint up down dev smoke migrate
setup:
	$(PYTHON) scripts/setup.py
	$(PYTHON) -m venv .venv
	.venv/bin/python -m pip install -r requirements-dev.txt
	.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
test:
	.venv/bin/python -m pytest -q
lint:
	.venv/bin/ruff check .
up:
	$(COMPOSE) up -d --build
down:
	$(COMPOSE) down
dev:
	$(COMPOSE) -f compose.yaml -f compose.dev.yaml up --build
migrate:
	$(COMPOSE) exec -T api alembic upgrade head
smoke:
	$(COMPOSE) exec -T api python scripts/smoke.py
