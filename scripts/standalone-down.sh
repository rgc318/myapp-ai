#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
REMOVE_VOLUMES=no

if [[ "${1:-}" == "--volumes" ]]; then
	REMOVE_VOLUMES=yes
elif [[ $# -gt 0 ]]; then
	echo "Usage: $0 [--volumes]" >&2
	exit 2
fi

ARGS=(
	--project-directory "${ROOT_DIR}"
	--env-file "${ROOT_DIR}/.env"
	-f "${ROOT_DIR}/compose.yaml"
	down
	--remove-orphans
)

if [[ "${REMOVE_VOLUMES}" == yes ]]; then
	ARGS+=(--volumes)
	echo "Removing standalone Redis and Qdrant volumes." >&2
fi

docker compose "${ARGS[@]}"
