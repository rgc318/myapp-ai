from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import statistics
import time
from typing import Awaitable, Callable

import httpx


@dataclass(slots=True)
class Sample:
	status_code: int
	latency_ms: float
	ok: bool
	error_code: str | None = None
	first_token_ms: float | None = None


def _percentile(values: list[float], percentile: float) -> float | None:
	if not values:
		return None
	ordered = sorted(values)
	position = (len(ordered) - 1) * percentile
	lower = math.floor(position)
	upper = math.ceil(position)
	if lower == upper:
		return round(ordered[lower], 2)
	weight = position - lower
	return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 2)


def _error_code(response: httpx.Response) -> str | None:
	try:
		body = response.json()
	except (json.JSONDecodeError, ValueError):
		return None
	detail = body.get("detail") if isinstance(body, dict) else None
	if isinstance(detail, dict):
		return str(detail.get("code") or "") or None
	return None


def _summary(samples: list[Sample], elapsed_seconds: float, *, concurrency: int | None = None) -> dict:
	latencies = [sample.latency_ms for sample in samples]
	successful_latencies = [sample.latency_ms for sample in samples if sample.ok]
	rejected_latencies = [sample.latency_ms for sample in samples if not sample.ok]
	first_tokens = [sample.first_token_ms for sample in samples if sample.first_token_ms is not None]
	statuses: dict[str, int] = {}
	errors: dict[str, int] = {}
	for sample in samples:
		statuses[str(sample.status_code)] = statuses.get(str(sample.status_code), 0) + 1
		if sample.error_code:
			errors[sample.error_code] = errors.get(sample.error_code, 0) + 1
	successes = sum(1 for sample in samples if sample.ok)
	def latency_summary(values: list[float]) -> dict | None:
		if not values:
			return None
		return {
			"min": round(min(values), 2),
			"mean": round(statistics.fmean(values), 2),
			"p50": _percentile(values, 0.50),
			"p95": _percentile(values, 0.95),
			"p99": _percentile(values, 0.99),
			"max": round(max(values), 2),
		}

	return {
		"concurrency": concurrency,
		"requests": len(samples),
		"successes": successes,
		"error_rate": round(1 - (successes / len(samples)), 6) if samples else 0,
		"elapsed_seconds": round(elapsed_seconds, 3),
		"throughput_rps": round(len(samples) / elapsed_seconds, 3) if elapsed_seconds else None,
		"latency_ms": latency_summary(latencies),
		"successful_latency_ms": latency_summary(successful_latencies),
		"rejected_latency_ms": latency_summary(rejected_latencies),
		"first_token_ms": {
			"p50": _percentile(first_tokens, 0.50),
			"p95": _percentile(first_tokens, 0.95),
			"p99": _percentile(first_tokens, 0.99),
		} if first_tokens else None,
		"status_counts": statuses,
		"error_codes": errors,
	}


async def _wave(
	operation: Callable[[], Awaitable[Sample]], *, concurrency: int, rounds: int,
) -> dict:
	samples: list[Sample] = []
	started = time.perf_counter()
	for _round in range(rounds):
		gate = asyncio.Event()

		async def invoke() -> Sample:
			await gate.wait()
			return await operation()

		tasks = [asyncio.create_task(invoke()) for _index in range(concurrency)]
		gate.set()
		samples.extend(await asyncio.gather(*tasks))
	return _summary(samples, time.perf_counter() - started, concurrency=concurrency)


def _chat_payload(*, scenario: str = "general") -> dict:
	content = "请返回一个简短的合成响应。"
	if scenario == "sales_order_draft":
		content = "给合成客户创建 2 个合成商品的销售订单草稿。"
	return {
		"messages": [{"role": "user", "content": content}],
		"user": "synthetic-load-user",
		"scenario": scenario,
		"policy_context": {"roles": ["System Manager"], "environment": "test"},
	}


async def _post_json(client: httpx.AsyncClient, path: str, payload: dict) -> Sample:
	started = time.perf_counter()
	try:
		response = await client.post(path, json=payload)
		latency = (time.perf_counter() - started) * 1000
		return Sample(
			status_code=response.status_code, latency_ms=latency,
			ok=200 <= response.status_code < 300, error_code=_error_code(response),
		)
	except httpx.HTTPError as error:
		return Sample(
			status_code=0, latency_ms=(time.perf_counter() - started) * 1000,
			ok=False, error_code=type(error).__name__,
		)


async def _stream_chat(client: httpx.AsyncClient) -> Sample:
	started = time.perf_counter()
	first_token_ms = None
	status_code = 0
	error_code = None
	completed = False
	try:
		async with client.stream("POST", "/internal/v1/chat/stream", json=_chat_payload()) as response:
			status_code = response.status_code
			if response.status_code >= 400:
				await response.aread()
				error_code = _error_code(response)
			else:
				async for line in response.aiter_lines():
					if not line.startswith("data:"):
						continue
					event = json.loads(line[5:].strip())
					if event.get("type") == "message_delta" and first_token_ms is None:
						first_token_ms = (time.perf_counter() - started) * 1000
					if event.get("type") == "completed":
						completed = True
					elif event.get("type") == "error":
						error_code = str(event.get("code") or "SSE_ERROR")
	except (httpx.HTTPError, json.JSONDecodeError) as error:
		error_code = type(error).__name__
	return Sample(
		status_code=status_code, latency_ms=(time.perf_counter() - started) * 1000,
		ok=status_code == 200 and completed, error_code=error_code,
		first_token_ms=first_token_ms,
	)


def _embedding_documents(size: int) -> list[dict]:
	return [
		{
			"item_code": f"LOAD-{size:03d}-{index:03d}",
			"text": f"合成压测商品 {index}",
			"content_hash": f"{index:064x}"[-64:],
			"index_version": "synthetic-load-v1",
			"disabled": 0, "is_sales_item": 1, "is_purchase_item": 1, "is_stock_item": 1,
			"company_scope": ["*"],
		}
		for index in range(size)
	]


async def run(args) -> dict:
	token = os.environ.get(args.service_token_env, "")
	if not token:
		raise RuntimeError(f"{args.service_token_env} is required")
	headers = {"Authorization": f"Bearer {token}"}
	limits = httpx.Limits(max_connections=512, max_keepalive_connections=256)
	timeout = httpx.Timeout(connect=5, read=args.timeout_seconds, write=args.timeout_seconds, pool=5)
	report = {
		"schema": "myapp-ai-load-report-v1",
		"generated_at": datetime.now(timezone.utc).isoformat(),
		"mode": args.mode,
		"provider_kind": args.provider_kind,
		"rounds": args.rounds,
		"synthetic_data_only": True,
		"captures_prompt_or_output": False,
		"scenarios": {},
	}
	async with httpx.AsyncClient(
		base_url=args.base_url.rstrip("/"), headers=headers, limits=limits, timeout=timeout,
	) as client:
		matrices = {
			"chat": [10, 20, 50, 100],
			"sse": [20, 50, 100, 200],
			"search": [20, 50, 100],
			"draft": [5, 10, 20],
		}
		if args.mode == "smoke":
			matrices = {"chat": [2], "sse": [2], "search": [2], "draft": [2]}
		for scenario in ("chat", "sse", "search", "draft"):
			raw_levels = getattr(args, f"{scenario}_levels")
			if raw_levels:
				levels = [int(value.strip()) for value in raw_levels.split(",") if value.strip()]
				if not levels or any(level < 1 for level in levels):
					raise RuntimeError(f"{scenario} levels must be positive integers")
				matrices[scenario] = levels
		selected_scenarios = {value.strip() for value in args.scenarios.split(",") if value.strip()}
		unknown = selected_scenarios - {*matrices, "embedding_batch"}
		if unknown:
			raise RuntimeError(f"Unknown scenarios: {', '.join(sorted(unknown))}")
		for scenario, levels in matrices.items():
			if scenario not in selected_scenarios:
				continue
			rows = []
			for level in levels:
				if scenario == "chat":
					operation = lambda: _post_json(client, "/internal/v1/chat", _chat_payload())
				elif scenario == "sse":
					operation = lambda: _stream_chat(client)
				elif scenario == "search":
					operation = lambda: _post_json(client, "/internal/v1/vector/products/search", {
						"query": "合成蓝色饮料", "item_context": "sales", "limit": 8,
					})
				else:
					operation = lambda: _post_json(
						client, "/internal/v1/drafts/sales-order", _chat_payload(scenario="sales_order_draft"),
					)
				rows.append(await _wave(operation, concurrency=level, rounds=args.rounds))
			report["scenarios"][scenario] = rows

		if "embedding_batch" in selected_scenarios:
			batch_sizes = [32, 64, 128] if args.mode == "full" else [2]
			batch_rows = []
			collection = f"myapp-products-loadtest-{args.run_id}"
			warmup_started = time.perf_counter()
			warmup = await _post_json(client, "/internal/v1/vector/products/upsert", {
				"documents": _embedding_documents(1),
				"embedding_model": args.embedding_model,
				"collection": collection,
			})
			report["scenarios"]["embedding_warmup"] = _summary(
				[warmup], time.perf_counter() - warmup_started,
			)
			for batch_size in batch_sizes:
				started = time.perf_counter()
				sample = await _post_json(client, "/internal/v1/vector/products/upsert", {
					"documents": _embedding_documents(batch_size),
					"embedding_model": args.embedding_model,
					"collection": collection,
				})
				batch_rows.append({
					"batch_size": batch_size,
					**_summary([sample], time.perf_counter() - started),
				})
			report["scenarios"]["embedding_batch"] = batch_rows

	if args.qdrant_url and "embedding_batch" in selected_scenarios:
		collection = f"myapp-products-loadtest-{args.run_id}"
		async with httpx.AsyncClient(base_url=args.qdrant_url.rstrip("/"), timeout=10) as qdrant:
			response = await qdrant.delete(f"/collections/{collection}")
			report["cleanup"] = {
				"collection": collection,
				"status_code": response.status_code,
				"ok": response.status_code in {200, 404},
			}
	return report


def main() -> int:
	parser = argparse.ArgumentParser(description="Run redacted MyApp AI concurrency baselines.")
	parser.add_argument("--base-url", required=True)
	parser.add_argument("--output", type=Path, required=True)
	parser.add_argument("--mode", choices=("smoke", "full"), default="smoke")
	parser.add_argument("--provider-kind", choices=("synthetic", "live"), default="synthetic")
	parser.add_argument(
		"--scenarios", default="chat,sse,search,draft,embedding_batch",
		help="Comma-separated subset of chat,sse,search,draft,embedding_batch.",
	)
	parser.add_argument("--chat-levels", default="")
	parser.add_argument("--sse-levels", default="")
	parser.add_argument("--search-levels", default="")
	parser.add_argument("--draft-levels", default="")
	parser.add_argument("--rounds", type=int, default=1)
	parser.add_argument("--timeout-seconds", type=float, default=90)
	parser.add_argument("--service-token-env", default="MYAPP_AI_SERVICE_TOKEN")
	parser.add_argument("--embedding-model", default="synthetic-embedding")
	parser.add_argument("--qdrant-url", default="")
	parser.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"))
	args = parser.parse_args()
	if args.rounds < 1:
		parser.error("--rounds must be at least 1")
	report = asyncio.run(run(args))
	args.output.parent.mkdir(parents=True, exist_ok=True)
	args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
	print(json.dumps({
		"output": str(args.output),
		"schema": report["schema"],
		"scenarios": sorted(report["scenarios"]),
	}, ensure_ascii=False))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
