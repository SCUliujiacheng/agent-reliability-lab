UV ?= uv
NPM ?= npm
DOCKER_COMPOSE ?= docker compose
ARTIFACTS_DIR ?= artifacts

.PHONY: install test test-python test-web lint typecheck build benchmark api web compose-up compose-down verify

install:
	$(UV) sync --locked --dev
	$(NPM) ci --prefix web

test: test-python test-web

test-python:
	$(UV) run pytest -v

test-web:
	$(NPM) --prefix web test -- --run

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(NPM) --prefix web run lint

typecheck:
	$(UV) run mypy src
	$(NPM) --prefix web run typecheck

build:
	$(NPM) --prefix web run build

benchmark:
	$(UV) run arl eval scenarios/incident-response --output $(ARTIFACTS_DIR)/current-report.json
	$(UV) run arl gate $(ARTIFACTS_DIR)/current-report.json --baseline benchmarks/baseline-report.json

api:
	$(UV) run uvicorn agent_reliability_lab.api.app:app --reload --host 127.0.0.1 --port 8000

web:
	$(NPM) --prefix web run dev

compose-up:
	$(DOCKER_COMPOSE) up --build --detach --wait

compose-down:
	$(DOCKER_COMPOSE) down

verify: test lint typecheck build benchmark
