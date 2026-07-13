from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid

import httpx

from ..config import Settings, get_settings
from ..litellm_client import LiteLLMClient
from ..prompts import get_prompt_spec
from ..schemas import ChatRequest
from .dataset import DatasetBundle, EvalConfigurationError, load_dataset, load_thresholds
from .graders import grade_output
from .models import EvalCase, ReplayResponse, ThresholdConfig


STRUCTURED_SCENARIOS = {
	"sales_order_draft",
	"purchase_order_draft",
	"inventory_adjustment_draft",
}
TRUTHY = {"1", "true", "yes", "on"}


def _utc_now() -> str:
	return datetime.now(timezone.utc).isoformat()


def _percentile(values: list[float], percentile: float) -> float:
	if not values:
		return 0.0
	ordered = sorted(values)
	index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
	return round(ordered[index], 3)


def _serialized_output(output: str | dict | None) -> str:
	if output is None:
		return ""
	if isinstance(output, str):
		return output
	return json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(slots=True)
class InvocationOutcome:
	output: str | dict | None
	trace_id: str | None
	model: str | None
	model_alias: str | None
	usage: dict
	latency_ms: float
	error_type: str | None = None


class ReplayHandler:
	def __init__(self, responses: list[ReplayResponse]):
		self.responses = list(responses)
		self.requests: list[dict] = []

	def __call__(self, request: httpx.Request) -> httpx.Response:
		try:
			self.requests.append(json.loads(request.content or b"{}"))
		except json.JSONDecodeError:
			self.requests.append({})
		if not self.responses:
			return httpx.Response(500, json={"error": "offline replay exhausted"})
		replay = self.responses.pop(0)
		if replay.body is not None:
			body = replay.body
		elif replay.status_code >= 400:
			body = {"error": {"message": "synthetic provider rejection"}}
		else:
			content = replay.content
			if isinstance(content, dict):
				content = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
			body = {
				"model": replay.model,
				"choices": [{"message": {"content": content or ""}, "finish_reason": "stop"}],
				"usage": replay.usage,
			}
		return httpx.Response(replay.status_code, json=body)


def _offline_settings(settings: Settings) -> Settings:
	return replace(
		settings,
		litellm_base_url="http://litellm.eval",
		litellm_api_key="offline-eval-key",
		model="offline-replay-model",
		langfuse_host="",
		langfuse_public_key="",
		langfuse_secret_key="",
	)


def _chat_request(case: EvalCase) -> ChatRequest:
	return ChatRequest(
		messages=case.request.messages,
		scenario=case.scenario,
		user=f"synthetic-eval:{case.id}",
		company=case.request.company,
		locale=case.request.locale,
		context=case.request.context,
		prompt_version=case.request.requested_prompt_version or get_prompt_spec(case.scenario).version,
		conversation_id=f"EVAL-{case.id}",
		run_id=f"EVAL-RUN-{case.id}",
	)


def _invoke_case(case: EvalCase, *, settings: Settings, mode: str) -> tuple[InvocationOutcome, LiteLLMClient]:
	if mode == "offline":
		handler = ReplayHandler(case.replay.responses)
		client = LiteLLMClient(
			_offline_settings(settings),
			transport=httpx.MockTransport(handler),
		)
	else:
		client = LiteLLMClient(settings)

	request = _chat_request(case)
	started = time.perf_counter()
	try:
		if case.scenario == "sales_order_draft":
			result = client.build_sales_order_draft(request)
			output = result.draft.model_dump(mode="json")
		elif case.scenario == "purchase_order_draft":
			result = client.build_purchase_order_draft(request)
			output = result.draft.model_dump(mode="json")
		elif case.scenario == "inventory_adjustment_draft":
			result = client.build_inventory_adjustment_draft(request)
			output = result.draft.model_dump(mode="json")
		else:
			result = client.chat(request)
			output = result.message.content
		return (
			InvocationOutcome(
				output=output,
				trace_id=result.trace_id,
				model=result.model,
				model_alias=result.model_alias,
				usage=result.usage.model_dump(mode="json"),
				latency_ms=round((time.perf_counter() - started) * 1000, 3),
			),
			client,
		)
	except Exception as error:
		return (
			InvocationOutcome(
				output=None,
				trace_id=None,
				model=None,
				model_alias=client.settings.model,
				usage={},
				latency_ms=round((time.perf_counter() - started) * 1000, 3),
				error_type=type(error).__name__,
			),
			client,
		)


def _filtered_cases(
	bundle: DatasetBundle,
	*,
	mode: str,
	case_ids: set[str] | None,
	tags: set[str] | None,
) -> list[EvalCase]:
	cases = [case for case in bundle.cases if mode in case.modes]
	if case_ids:
		available_case_ids = {case.id for case in cases}
		missing_case_ids = sorted(case_ids - available_case_ids)
		if missing_case_ids:
			raise EvalConfigurationError(
				f"Unknown evaluation case ids for {mode}: {', '.join(missing_case_ids)}"
			)
		cases = [case for case in cases if case.id in case_ids]
	if tags:
		cases = [case for case in cases if tags.intersection(case.tags)]
	if not cases:
		raise EvalConfigurationError("No evaluation cases matched the selected filters")
	return cases


def _aggregate_metrics(case_results: list[dict]) -> dict[str, float | None]:
	weighted: dict[str, list[float]] = {}
	critical_passes = []
	normal_passes = []
	for case_result in case_results:
		for attempt in case_result["attempts"]:
			metrics = attempt["scores"]
			weights = attempt.get("score_weights") or {}
			for name, value in metrics.items():
				weight = float(weights.get(name, 1.0))
				bucket = weighted.setdefault(name, [0.0, 0.0])
				bucket[0] += float(value) * weight
				bucket[1] += weight
			if case_result["severity"] == "critical":
				critical_passes.append(float(metrics.get("case_pass", 0)))
			else:
				normal_passes.append(float(metrics.get("case_pass", 0)))

	averages = {
		name: round(total / weight, 6)
		for name, (total, weight) in weighted.items()
		if weight
	}
	return {
		"critical_case_pass_rate": round(sum(critical_passes) / len(critical_passes), 6)
		if critical_passes else None,
		"normal_case_pass_rate": round(sum(normal_passes) / len(normal_passes), 6)
		if normal_passes else None,
		"schema_valid_rate": averages.get("schema_valid"),
		"safety_pass_rate": averages.get("safety_pass"),
		"forbidden_pattern_pass_rate": averages.get("forbidden_pattern_pass"),
		"structured_field_accuracy": averages.get("structured_field_accuracy"),
		"required_concept_recall": averages.get("required_concept_recall"),
		"grounded_identifier_precision": averages.get("grounded_identifier_precision"),
		"overall_attempt_pass_rate": averages.get("case_pass"),
	}


def _evaluate_thresholds(
	metrics: dict[str, float | None],
	*,
	mode: str,
	thresholds: ThresholdConfig,
	partial: bool,
) -> tuple[dict[str, float], list[str]]:
	selected = getattr(thresholds, mode).model_dump()
	failures = []
	for name, minimum in selected.items():
		value = metrics.get(name)
		if value is None:
			if not partial:
				failures.append(f"metric_missing:{name}")
			continue
		if value + 1e-12 < minimum:
			failures.append(f"metric_below_threshold:{name}")
	return selected, failures


def run_evaluation(
	*,
	settings: Settings,
	mode: str,
	dataset: DatasetBundle,
	thresholds: ThresholdConfig,
	repeat: int = 1,
	include_content: bool = False,
	case_ids: set[str] | None = None,
	tags: set[str] | None = None,
	sync_langfuse_scores: bool = True,
) -> dict:
	if mode not in {"offline", "live"}:
		raise EvalConfigurationError(f"Unsupported evaluation mode: {mode}")
	if repeat < 1 or repeat > 10:
		raise EvalConfigurationError("repeat must be between 1 and 10")
	if mode == "live" and not settings.litellm_api_key:
		raise EvalConfigurationError("MYAPP_AI_LITELLM_API_KEY is required for live evaluations")

	selected_cases = _filtered_cases(
		dataset,
		mode=mode,
		case_ids=case_ids,
		tags=tags,
	)
	full_mode_case_count = sum(1 for case in dataset.cases if mode in case.modes)
	partial_gate = len(selected_cases) != full_mode_case_count
	run_id = str(uuid.uuid4())
	started_at = _utc_now()
	case_results = []
	latencies = []
	token_totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "reasoning_tokens": 0}

	for case in selected_cases:
		attempt_results = []
		for attempt_number in range(1, repeat + 1):
			outcome, client = _invoke_case(case, settings=settings, mode=mode)
			grade = grade_output(case, output=outcome.output, error_type=outcome.error_type)
			serialized = _serialized_output(outcome.output)
			latencies.append(outcome.latency_ms)
			for token_name in token_totals:
				token_totals[token_name] += int(outcome.usage.get(token_name) or 0)
			observability_synced = False
			if (
				mode == "live"
				and sync_langfuse_scores
				and outcome.trace_id
			):
				observability_synced = client.langfuse.record_evaluation_scores(
					trace_id=outcome.trace_id,
					case_id=case.id,
					dataset_version=case.dataset_version,
					prompt_version=get_prompt_spec(case.scenario).version,
					mode=mode,
					attempt=attempt_number,
					scores=grade.metrics,
				)
			attempt_result = {
				"attempt": attempt_number,
				"passed": grade.passed,
				"prompt_version": get_prompt_spec(case.scenario).version,
				"trace_id": outcome.trace_id,
				"model": outcome.model,
				"model_alias": outcome.model_alias,
				"latency_ms": outcome.latency_ms,
				"usage": outcome.usage,
				"scores": grade.metrics,
				"score_weights": grade.weights,
				"failures": grade.failures,
				"error_type": outcome.error_type,
				"output_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
				"output_chars": len(serialized),
				"observability_synced": observability_synced,
			}
			if include_content:
				attempt_result["output"] = outcome.output
			attempt_results.append(attempt_result)
		output_hashes = {attempt["output_sha256"] for attempt in attempt_results}
		case_results.append(
			{
				"id": case.id,
				"scenario": case.scenario,
				"severity": case.severity,
				"tags": case.tags,
				"passed": all(attempt["passed"] for attempt in attempt_results),
				"stable_output": len(output_hashes) == 1,
				"attempts": attempt_results,
			}
		)

	metrics = _aggregate_metrics(case_results)
	threshold_values, threshold_failures = _evaluate_thresholds(
		metrics,
		mode=mode,
		thresholds=thresholds,
		partial=partial_gate,
	)
	completed_at = _utc_now()
	attempt_count = len(selected_cases) * repeat
	passed_attempt_count = sum(
		1 for case_result in case_results for attempt in case_result["attempts"] if attempt["passed"]
	)
	return {
		"schema_version": "myapp-ai-eval-report-v1",
		"run_id": run_id,
		"mode": mode,
		"started_at": started_at,
		"completed_at": completed_at,
		"release": settings.langfuse_release or None,
		"environment": settings.langfuse_environment,
		"content_included": include_content,
		"dataset": {
			"name": dataset.name,
			"version": dataset.version,
			"sha256": dataset.sha256,
			"case_count": len(selected_cases),
		},
		"prompt_versions": {
			scenario: get_prompt_spec(scenario).version
			for scenario in sorted({case.scenario for case in selected_cases})
		},
		"summary": {
			"passed": not threshold_failures,
			"gate_scope": "partial" if partial_gate else "full",
			"release_gate_eligible": not partial_gate,
			"case_count": len(selected_cases),
			"attempt_count": attempt_count,
			"passed_attempt_count": passed_attempt_count,
			"all_attempts_pass_rate": round(passed_attempt_count / attempt_count, 6),
			"all_attempts_stable_rate": round(
				sum(1 for result in case_results if result["stable_output"]) / len(case_results),
				6,
			),
			"metrics": metrics,
			"thresholds": threshold_values,
			"threshold_failures": threshold_failures,
			"latency_ms": {
				"p50": _percentile(latencies, 0.50),
				"p95": _percentile(latencies, 0.95),
				"max": round(max(latencies), 3) if latencies else 0.0,
			},
			"tokens": token_totals,
		},
		"cases": case_results,
	}


def _write_report(report: dict, output: str) -> None:
	serialized = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
	if output == "-":
		print(serialized)
		return
	path = Path(output)
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(f"{serialized}\n", encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Run synthetic fixed evaluations for myapp AI")
	parser.add_argument("--mode", choices=("offline", "live"), default="offline")
	parser.add_argument("--dataset", default="core")
	parser.add_argument("--thresholds", default="thresholds")
	parser.add_argument("--repeat", type=int, default=1)
	parser.add_argument("--output", default="-")
	parser.add_argument("--case", action="append", dest="case_ids")
	parser.add_argument("--tag", action="append", dest="tags")
	parser.add_argument("--include-content", action="store_true")
	parser.add_argument("--no-langfuse-scores", action="store_true")
	return parser


def main(argv: list[str] | None = None) -> int:
	args = _build_parser().parse_args(argv)
	try:
		if args.mode == "live" and os.environ.get("MYAPP_AI_ENABLE_LIVE_EVALS", "0").lower() not in TRUTHY:
			raise EvalConfigurationError(
				"Live evaluations are billable; set MYAPP_AI_ENABLE_LIVE_EVALS=1 explicitly"
			)
		report = run_evaluation(
			settings=get_settings(),
			mode=args.mode,
			dataset=load_dataset(args.dataset),
			thresholds=load_thresholds(args.thresholds),
			repeat=args.repeat,
			include_content=args.include_content,
			case_ids=set(args.case_ids or []),
			tags=set(args.tags or []),
			sync_langfuse_scores=not args.no_langfuse_scores,
		)
		_write_report(report, args.output)
		summary = report["summary"]
		gate_label = "PASS" if summary["passed"] else "FAIL"
		if not summary["release_gate_eligible"]:
			gate_label = f"PARTIAL_{gate_label}"
		print(
			f"AI eval {args.mode}: {summary['passed_attempt_count']}/{summary['attempt_count']} attempts passed; "
			f"gate={gate_label}",
			file=sys.stderr,
		)
		return 0 if summary["passed"] else 1
	except EvalConfigurationError as error:
		print(f"AI eval configuration error: {error}", file=sys.stderr)
		return 2
	except Exception as error:
		print(f"AI eval runtime error: {type(error).__name__}", file=sys.stderr)
		return 2


if __name__ == "__main__":
	raise SystemExit(main())
