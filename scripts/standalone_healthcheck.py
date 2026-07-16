from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _read_env(path: Path) -> dict[str, str]:
	values: dict[str, str] = {}
	for raw_line in path.read_text(encoding="utf-8").splitlines():
		line = raw_line.strip()
		if not line or line.startswith("#") or "=" not in line:
			continue
		key, value = line.split("=", 1)
		values[key.strip()] = value.strip()
	return values


def _request(url: str, *, token: str = "", payload: dict | None = None) -> dict:
	body = None
	headers = {"Accept": "application/json"}
	if token:
		headers["Authorization"] = f"Bearer {token}"
	if payload is not None:
		body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
		headers["Content-Type"] = "application/json"
	request = Request(url, data=body, headers=headers, method="POST" if body else "GET")
	with urlopen(request, timeout=10) as response:
		return json.loads(response.read())


def _wait_for_health(base_url: str, timeout_seconds: int) -> dict:
	deadline = time.monotonic() + timeout_seconds
	last_error: Exception | None = None
	while time.monotonic() < deadline:
		try:
			payload = _request(f"{base_url}/health")
			if payload.get("status") == "ok":
				return payload
		except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
			last_error = error
		time.sleep(1)
	raise RuntimeError(f"AI health check did not become ready: {last_error}")


def _chat(base_url: str, token: str) -> None:
	payload = _request(
		f"{base_url}/internal/v1/chat",
		token=token,
		payload={
			"messages": [{"role": "user", "content": "返回合成响应"}],
			"scenario": "general",
			"user": "integration@example.invalid",
			"policy_context": {"roles": [], "environment": "test"},
		},
	)
	if payload.get("message", {}).get("content") != "合成响应":
		raise RuntimeError(f"Unexpected synthetic chat response: {payload}")


def _vector(base_url: str, token: str) -> None:
	item_code = "INTEGRATION-SKU-001"
	text = "合成集成测试商品"
	content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
	upsert = _request(
		f"{base_url}/internal/v1/vector/products/upsert",
		token=token,
		payload={
			"documents": [{
				"item_code": item_code,
				"text": text,
				"content_hash": content_hash,
				"index_version": "integration-v1",
			}],
		},
	)
	if upsert.get("indexed_count") != 1:
		raise RuntimeError(f"Synthetic vector upsert failed: {upsert}")
	search = _request(
		f"{base_url}/internal/v1/vector/products/search",
		token=token,
		payload={"query": text, "limit": 3, "item_context": "all"},
	)
	if not any(row.get("item_code") == item_code for row in search.get("matches", [])):
		raise RuntimeError(f"Synthetic vector search failed: {search}")
	deleted = _request(
		f"{base_url}/internal/v1/vector/products/delete",
		token=token,
		payload={"item_codes": [item_code]},
	)
	if deleted.get("deleted_count") != 1:
		raise RuntimeError(f"Synthetic vector delete failed: {deleted}")


def main() -> int:
	parser = argparse.ArgumentParser(description="Validate a standalone MyApp AI deployment.")
	parser.add_argument("--base-url", default="http://127.0.0.1:4010")
	parser.add_argument("--env-file", default=".env")
	parser.add_argument("--timeout-seconds", type=int, default=60)
	parser.add_argument("--chat", action="store_true")
	parser.add_argument("--vector", action="store_true")
	args = parser.parse_args()

	env = _read_env(Path(args.env_file))
	token = env.get("MYAPP_AI_SERVICE_TOKEN", "")
	health = _wait_for_health(args.base_url.rstrip("/"), args.timeout_seconds)
	if args.chat:
		_chat(args.base_url.rstrip("/"), token)
	if args.vector:
		_vector(args.base_url.rstrip("/"), token)
	print(json.dumps({"health": health, "chat": args.chat, "vector": args.vector}, ensure_ascii=False))
	return 0


if __name__ == "__main__":
	try:
		raise SystemExit(main())
	except (HTTPError, URLError, RuntimeError, ValueError) as error:
		raise SystemExit(str(error)) from error
