#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ ! -f "${ROOT_DIR}/.env" ]]; then
	echo "Missing ${ROOT_DIR}/.env; copy .env.example to .env and replace all change-me values." >&2
	exit 1
fi

if grep -Eq '(^|=)change-me' "${ROOT_DIR}/.env"; then
	echo ".env still contains change-me placeholders." >&2
	exit 1
fi

mkdir -p "${ROOT_DIR}/runtime/governance-reports"

docker compose \
	--project-directory "${ROOT_DIR}" \
	--env-file "${ROOT_DIR}/.env" \
	-f "${ROOT_DIR}/compose.yaml" \
	config --quiet

docker compose \
	--project-directory "${ROOT_DIR}" \
	--env-file "${ROOT_DIR}/.env" \
	-f "${ROOT_DIR}/compose.yaml" \
	up -d --build --wait

python3 "${ROOT_DIR}/scripts/standalone_healthcheck.py"
