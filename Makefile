.PHONY: config test lint build up down down-volumes health integration

config:
	docker compose --env-file .env config --quiet

test:
	docker build --target test -t myapp-ai:test .
	docker run --rm myapp-ai:test

lint:
	uv sync --extra test --extra dev --frozen
	uv run ruff check .

build:
	docker build --target runtime -t myapp-ai:runtime .

up:
	./scripts/standalone-up.sh

down:
	./scripts/standalone-down.sh

down-volumes:
	./scripts/standalone-down.sh --volumes

health:
	python3 scripts/standalone_healthcheck.py

integration:
	MYAPP_AI_ENV_FILE=integration.env docker compose --env-file integration.env -f compose.yaml -f compose.integration.yaml up -d --build --wait
	python3 scripts/standalone_healthcheck.py --env-file integration.env --chat --vector
	MYAPP_AI_ENV_FILE=integration.env docker compose --env-file integration.env -f compose.yaml -f compose.integration.yaml down --remove-orphans --volumes
