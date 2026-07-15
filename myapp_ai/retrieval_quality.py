from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib import resources
import json
import math
import os
from pathlib import Path
import re
import sys
import time

import httpx


REPORT_SCHEMA_VERSION = "myapp-ai-product-retrieval-quality-report-v1"
DEFAULT_DATASET = "product_retrieval_zh_cn.v1.json"
TRUTHY = {"1", "true", "yes", "on"}


class RetrievalQualityConfigurationError(RuntimeError):
	pass


def _utc_now() -> str:
	return datetime.now(timezone.utc).isoformat()


def _percentile(values: list[float], percentile: float) -> float:
	if not values:
		return 0.0
	ordered = sorted(values)
	index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
	return round(ordered[index], 3)


def _excluded_prefixes(raw_value: str) -> tuple[str, ...]:
	values = re.split(r"[,;\n]", str(raw_value or ""))
	return tuple(dict.fromkeys(value.strip().casefold() for value in values if value.strip()))


def load_retrieval_dataset(name_or_path: str = DEFAULT_DATASET) -> dict:
	path = Path(name_or_path)
	if path.is_file():
		text = path.read_text(encoding="utf-8")
		source = str(path)
	else:
		resource = resources.files("myapp_ai.evals.datasets").joinpath(name_or_path)
		if not resource.is_file():
			raise RetrievalQualityConfigurationError(f"Retrieval dataset not found: {name_or_path}")
		text = resource.read_text(encoding="utf-8")
		source = name_or_path
	try:
		payload = json.loads(text)
	except json.JSONDecodeError as error:
		raise RetrievalQualityConfigurationError(f"Invalid retrieval dataset JSON: {source}") from error
	if payload.get("schema_version") != "myapp-ai-product-retrieval-dataset-v1":
		raise RetrievalQualityConfigurationError(f"Unsupported retrieval dataset schema: {source}")
	cases = payload.get("cases")
	if not isinstance(cases, list) or not cases:
		raise RetrievalQualityConfigurationError(f"Retrieval dataset is empty: {source}")
	case_ids = set()
	for case in cases:
		if not isinstance(case, dict):
			raise RetrievalQualityConfigurationError(f"Invalid retrieval case in {source}")
		case_id = str(case.get("id") or "")
		query = str(case.get("query") or "").strip()
		expected = str(case.get("expected_item_code") or "").strip()
		if not case_id or not query or not expected or case_id in case_ids:
			raise RetrievalQualityConfigurationError(f"Invalid or duplicate retrieval case: {case_id or '<missing>'}")
		case_ids.add(case_id)
	return {
		**payload,
		"source": source,
		"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
	}


def run_retrieval_quality(
	*, client: httpx.Client, dataset: dict, excluded_prefixes: tuple[str, ...] = (),
	top1_minimum: float = 0.9, top3_minimum: float = 1.0,
) -> dict:
	if not 0 <= top1_minimum <= 1 or not 0 <= top3_minimum <= 1:
		raise RetrievalQualityConfigurationError("Retrieval thresholds must be between 0 and 1")
	results = []
	latencies = []
	provider_errors = 0
	excluded_candidate_count = 0
	top1_hits = 0
	top3_hits = 0
	models = set()
	collections = set()
	for case in dataset["cases"]:
		started = time.perf_counter()
		codes = []
		error_code = None
		try:
			response = client.post(
				"/internal/v1/vector/products/search",
				json={"query": case["query"], "limit": 3, "item_context": "all"},
			)
			response.raise_for_status()
			payload = response.json()
			codes = [str(row.get("item_code") or "") for row in payload.get("matches") or []]
			if payload.get("embedding_model"):
				models.add(str(payload["embedding_model"]))
			if payload.get("collection"):
				collections.add(str(payload["collection"]))
		except httpx.HTTPStatusError as error:
			provider_errors += 1
			error_code = f"HTTP_{error.response.status_code}"
		except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError) as error:
			provider_errors += 1
			error_code = type(error).__name__
		latency_ms = round((time.perf_counter() - started) * 1000, 3)
		latencies.append(latency_ms)
		expected = case["expected_item_code"]
		top1 = bool(codes and codes[0] == expected)
		top3 = expected in codes[:3]
		excluded_hits = [
			code for code in codes
			if any(code.casefold().startswith(prefix) for prefix in excluded_prefixes)
		]
		top1_hits += int(top1)
		top3_hits += int(top3)
		excluded_candidate_count += len(excluded_hits)
		results.append({
			"id": case["id"],
			"query": case["query"],
			"expected_item_code": expected,
			"candidate_item_codes": codes,
			"top1_hit": top1,
			"top3_hit": top3,
			"excluded_candidate_item_codes": excluded_hits,
			"latency_ms": latency_ms,
			"error_code": error_code,
		})
	case_count = len(results)
	top1_rate = round(top1_hits / case_count, 6)
	top3_rate = round(top3_hits / case_count, 6)
	failures = []
	if top1_rate < top1_minimum:
		failures.append("top1_rate_below_threshold")
	if top3_rate < top3_minimum:
		failures.append("top3_rate_below_threshold")
	if provider_errors:
		failures.append("provider_errors_present")
	if excluded_candidate_count:
		failures.append("excluded_candidates_present")
	return {
		"schema_version": REPORT_SCHEMA_VERSION,
		"generated_at": _utc_now(),
		"dataset": {
			"version": dataset.get("version"),
			"source": dataset.get("source"),
			"sha256": dataset.get("sha256"),
			"case_count": case_count,
			"synthetic": bool(dataset.get("synthetic")),
		},
		"runtime": {
			"embedding_models": sorted(models),
			"collections": sorted(collections),
			"excluded_item_prefixes": list(excluded_prefixes),
		},
		"summary": {
			"passed": not failures,
			"top1_rate": top1_rate,
			"top3_rate": top3_rate,
			"top1_minimum": top1_minimum,
			"top3_minimum": top3_minimum,
			"provider_error_count": provider_errors,
			"excluded_candidate_count": excluded_candidate_count,
			"latency_p50_ms": _percentile(latencies, 0.5),
			"latency_p95_ms": _percentile(latencies, 0.95),
			"threshold_failures": failures,
		},
		"cases": results,
	}


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Run the versioned Chinese product retrieval quality gate.")
	parser.add_argument("--dataset", default=DEFAULT_DATASET)
	parser.add_argument("--output")
	parser.add_argument("--top1-minimum", type=float, default=0.9)
	parser.add_argument("--top3-minimum", type=float, default=1.0)
	parser.add_argument("--allow-live", action="store_true")
	args = parser.parse_args(argv)
	if not args.allow_live and os.environ.get("MYAPP_AI_ENABLE_LIVE_EVALS", "0").casefold() not in TRUTHY:
		print("Live retrieval evaluation is disabled; pass --allow-live or set MYAPP_AI_ENABLE_LIVE_EVALS=1.", file=sys.stderr)
		return 2
	base_url = os.environ.get("MYAPP_AI_ORCHESTRATOR_URL", "http://ai-orchestrator:4010").rstrip("/")
	token = os.environ.get("MYAPP_AI_SERVICE_TOKEN", "").strip()
	if not token:
		print("MYAPP_AI_SERVICE_TOKEN is required.", file=sys.stderr)
		return 2
	try:
		dataset = load_retrieval_dataset(args.dataset)
		with httpx.Client(
			base_url=base_url,
			headers={"Authorization": f"Bearer {token}"},
			timeout=30,
		) as client:
			report = run_retrieval_quality(
				client=client,
				dataset=dataset,
				excluded_prefixes=_excluded_prefixes(
					os.environ.get("MYAPP_AI_VECTOR_EXCLUDED_ITEM_PREFIXES", "")
				),
				top1_minimum=args.top1_minimum,
				top3_minimum=args.top3_minimum,
			)
	except RetrievalQualityConfigurationError as error:
		print(str(error), file=sys.stderr)
		return 2
	serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
	if args.output:
		output = Path(args.output)
		output.parent.mkdir(parents=True, exist_ok=True)
		output.write_text(serialized, encoding="utf-8")
	else:
		print(serialized, end="")
	return 0 if report["summary"]["passed"] else 1


if __name__ == "__main__":
	raise SystemExit(main())
