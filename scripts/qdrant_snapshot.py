from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx


def _status(client: httpx.Client, collection: str) -> dict:
	response = client.get(f"/collections/{collection}")
	response.raise_for_status()
	result = response.json().get("result") or {}
	vectors = (((result.get("config") or {}).get("params") or {}).get("vectors") or {})
	return {
		"collection": collection,
		"points_count": int(result.get("points_count") or 0),
		"indexed_vectors_count": int(result.get("indexed_vectors_count") or 0),
		"vector_size": int(vectors.get("size")) if vectors.get("size") else None,
	}


def backup(client: httpx.Client, collection: str, output: Path) -> dict:
	created = client.post(f"/collections/{collection}/snapshots", params={"wait": "true"})
	created.raise_for_status()
	name = str((created.json().get("result") or {}).get("name") or "")
	if not name:
		raise RuntimeError("Qdrant did not return a snapshot name")
	output.parent.mkdir(parents=True, exist_ok=True)
	with client.stream("GET", f"/collections/{collection}/snapshots/{name}") as response:
		response.raise_for_status()
		with output.open("wb") as handle:
			for chunk in response.iter_bytes():
				handle.write(chunk)
	return {"snapshot_name": name, "snapshot_file": output.name, **_status(client, collection)}


def restore(client: httpx.Client, collection: str, snapshot: Path) -> dict:
	with snapshot.open("rb") as handle:
		response = client.post(
			f"/collections/{collection}/snapshots/upload",
			params={"priority": "snapshot"},
			files={"snapshot": (snapshot.name, handle, "application/octet-stream")},
		)
	response.raise_for_status()
	return _status(client, collection)


def main() -> int:
	parser = argparse.ArgumentParser(description="Create and restore MyApp Qdrant snapshots.")
	parser.add_argument("action", choices=("backup", "restore", "status", "delete"))
	parser.add_argument("--url", required=True)
	parser.add_argument("--collection", required=True)
	parser.add_argument("--output", type=Path)
	parser.add_argument("--snapshot", type=Path)
	args = parser.parse_args()
	with httpx.Client(base_url=args.url.rstrip("/"), timeout=120) as client:
		if args.action == "backup":
			if not args.output:
				parser.error("backup requires --output")
			result = backup(client, args.collection, args.output)
		elif args.action == "restore":
			if not args.snapshot:
				parser.error("restore requires --snapshot")
			result = restore(client, args.collection, args.snapshot)
		elif args.action == "status":
			result = _status(client, args.collection)
		else:
			response = client.delete(f"/collections/{args.collection}")
			if response.status_code not in {200, 404}:
				response.raise_for_status()
			result = {"collection": args.collection, "deleted": response.status_code == 200}
	print(json.dumps(result, ensure_ascii=False))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
