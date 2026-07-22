from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from .config import Settings
from .evals.dataset import load_dataset, load_thresholds
from .evals.runner import run_evaluation
from .prompts import get_prompt_spec


def _litellm_model_ids(settings: Settings, transport: httpx.BaseTransport | None = None) -> set[str]:
	if not settings.litellm_api_key:
		raise RuntimeError("MYAPP_AI_LITELLM_API_KEY is not configured")
	with httpx.Client(
		base_url=settings.litellm_base_url,
		timeout=settings.timeout_seconds,
		transport=transport,
	) as client:
		response = client.get(
			"/v1/models",
			headers={
				"Authorization": f"Bearer {settings.litellm_api_key}",
				"Cache-Control": "no-cache",
				"Pragma": "no-cache",
			},
		)
		response.raise_for_status()
		body = response.json()
	return {
		str(item.get("id") or "").strip()
		for item in body.get("data", [])
		if isinstance(item, dict) and str(item.get("id") or "").strip()
	}


def discover_models(settings: Settings, transport: httpx.BaseTransport | None = None) -> list[dict]:
	available = _litellm_model_ids(settings, transport=transport)

	def capability_for(alias: str) -> str:
		lowered = alias.casefold()
		if alias == settings.embedding_model or "embedding" in lowered or "embed" in lowered:
			return "embedding"
		return "fast_chat"

	def serialize(alias: str) -> dict:
		capability = capability_for(alias)
		is_available = alias in available
		return {
			"model_alias": alias,
			"capability": capability,
			"status": "active" if is_available else "degraded",
			"provider_family": "litellm",
			"provider_model_display": alias,
			"supports_streaming": capability != "embedding",
			"supports_json_schema": False,
			"supports_vision": False,
			"embedding_dimensions": None,
			"embedding_space_version": settings.qdrant_collection if capability == "embedding" else None,
			"data_region": None,
			"retention_policy": "master-data-only" if capability == "embedding" else "managed-by-provider",
			"sensitive_data_allowed": False,
			"input_cost": 0,
			"output_cost": 0,
			"currency": None,
			"last_health_status": "listed" if is_available else "missing",
			"last_error_code": None if is_available else "MODEL_ALIAS_NOT_FOUND",
		}

	ordered_aliases = []
	if settings.model:
		ordered_aliases.append(settings.model)
	ordered_aliases.extend(sorted(
		alias for alias in available
		if alias not in {settings.model, settings.embedding_model}
	))
	if settings.embedding_model:
		ordered_aliases.append(settings.embedding_model)
	return [serialize(alias) for alias in dict.fromkeys(ordered_aliases) if alias]


def _probe_model(
	settings: Settings,
	model: dict,
	transport: httpx.BaseTransport | None = None,
) -> dict:
	alias = str(model["model_alias"])
	capability = str(model["capability"])
	started_at = time.monotonic()
	provider_model = None
	error_code = None
	available = False
	try:
		with httpx.Client(
			base_url=settings.litellm_base_url,
			timeout=max(5, min(settings.timeout_seconds, 20)),
			transport=transport,
		) as client:
			if capability == "embedding":
				response = client.post(
					"/v1/embeddings",
					headers={"Authorization": f"Bearer {settings.litellm_api_key}"},
					json={"model": alias, "input": ["myapp availability check"]},
				)
			else:
				response = client.post(
					"/v1/chat/completions",
					headers={"Authorization": f"Bearer {settings.litellm_api_key}"},
					json={
						"model": alias,
						"messages": [{"role": "user", "content": "Reply only OK."}],
						"max_completion_tokens": 8,
						"stream": False,
					},
				)
			response.raise_for_status()
			body = response.json()
			provider_model = str(body.get("model") or "").strip() or None
			if capability == "embedding":
				available = bool(body.get("data"))
			else:
				choices = body.get("choices") or []
				available = bool(choices and isinstance(choices[0], dict) and choices[0].get("message"))
			if not available:
				error_code = "EMPTY_PROVIDER_RESPONSE"
	except httpx.HTTPStatusError as error:
		error_code = f"PROVIDER_HTTP_{error.response.status_code}"
	except httpx.TimeoutException:
		error_code = "PROVIDER_TIMEOUT"
	except (httpx.HTTPError, ValueError, RuntimeError, TypeError) as error:
		error_code = type(error).__name__.upper()
	return {
		"model_alias": alias,
		"capability": capability,
		"available": available,
		"latency_ms": round((time.monotonic() - started_at) * 1000, 3),
		"provider_model": provider_model,
		"error_code": error_code,
	}


def check_model_availability(
	settings: Settings,
	model_aliases: list[str] | None = None,
	transport: httpx.BaseTransport | None = None,
) -> dict:
	models = discover_models(settings, transport=transport)
	by_alias = {model["model_alias"]: model for model in models}
	aliases = list(dict.fromkeys(model_aliases or by_alias.keys()))
	results_by_alias = {}
	probe_models = []
	for alias in aliases:
		model = by_alias.get(alias)
		if not model or model.get("status") != "active":
			results_by_alias[alias] = {
				"model_alias": alias,
				"capability": model.get("capability") if model else None,
				"available": False,
				"latency_ms": 0,
				"provider_model": None,
				"error_code": "MODEL_ALIAS_NOT_LISTED",
			}
			continue
		probe_models.append(model)

	if probe_models:
		with ThreadPoolExecutor(max_workers=min(4, len(probe_models))) as executor:
			for result in executor.map(
				lambda model: _probe_model(settings, model, transport=transport),
				probe_models,
			):
				results_by_alias[result["model_alias"]] = result

	items = [results_by_alias[alias] for alias in aliases]
	available_count = sum(1 for item in items if item["available"])
	return {
		"source": "litellm",
		"checked_count": len(items),
		"available_count": available_count,
		"unavailable_count": len(items) - available_count,
		"items": items,
	}


def _load_gate_report(
	path_value: str, *, expected_mode: str | None = None,
	expected_schema: str = "myapp-ai-eval-report-v1",
) -> tuple[dict | None, list[str]]:
	if not path_value:
		return None, ["A governed full-gate report path is not configured"]
	path = Path(path_value)
	if not path.is_file():
		return None, ["The configured governed full-gate report does not exist"]
	try:
		report = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError):
		return None, ["The configured governed full-gate report is invalid"]
	errors = []
	if report.get("schema_version") != expected_schema:
		errors.append("The governed report schema is not supported")
	if expected_mode and report.get("mode") != expected_mode:
		errors.append(f"The governed report must use {expected_mode} mode")
	summary = report.get("summary") or {}
	if summary.get("gate_scope") != "full" or not summary.get("release_gate_eligible"):
		errors.append("The governed report is not a full release gate")
	if not summary.get("passed") or summary.get("threshold_failures"):
		errors.append("The governed report did not pass its thresholds")
	return report, errors


def _report_uses_model(report: dict, model_alias: str) -> bool:
	aliases = {
		str(attempt.get("model_alias") or "")
		for case in report.get("cases", [])
		for attempt in case.get("attempts", [])
		if isinstance(attempt, dict)
	}
	return model_alias in aliases


def validate_policy(settings: Settings, policy: dict) -> dict:
	errors = []
	warnings = []
	scenario = str(policy.get("scenario") or "")
	capability = str(policy.get("capability") or "")
	primary_model_alias = str(policy.get("primary_model_alias") or "")
	try:
		prompt_spec = get_prompt_spec(scenario)
	except ValueError:
		errors.append("The policy scenario has no registered prompt")
		prompt_spec = None

	try:
		models = discover_models(settings)
	except (httpx.HTTPError, RuntimeError, ValueError):
		models = []
		errors.append("LiteLLM model discovery failed")
	by_alias = {model["model_alias"]: model for model in models}
	for alias in [primary_model_alias, *(policy.get("fallback_model_aliases") or [])]:
		model = by_alias.get(alias)
		if not model:
			errors.append(f"Model alias {alias} is not configured in this Orchestrator")
			continue
		if model["status"] not in {"validated", "active"}:
			errors.append(f"Model alias {alias} is not healthy")

	offline_report = run_evaluation(
		settings=settings,
		mode="offline",
		dataset=load_dataset(),
		thresholds=load_thresholds(),
		sync_langfuse_scores=False,
	)
	offline_summary = offline_report["summary"]
	if not offline_summary["passed"] or not offline_summary["release_gate_eligible"]:
		errors.append("The deterministic offline full gate did not pass")

	if capability == "embedding":
		gate_report, gate_errors = _load_gate_report(settings.governance_embedding_gate_report_path)
		if primary_model_alias != settings.embedding_model:
			gate_errors.append("The embedding policy does not match the configured embedding alias")
		if not settings.vector_search_enabled:
			gate_errors.append("Vector search is not fully configured")
	else:
		gate_report, gate_errors = _load_gate_report(
			settings.governance_live_gate_report_path,
			expected_mode="live",
		)
		if gate_report and not _report_uses_model(gate_report, primary_model_alias):
			gate_errors.append("The live full gate was not executed with the policy primary model")
	errors.extend(gate_errors)

	return {
		"release_gate_eligible": not errors,
		"errors": errors,
		"warnings": warnings,
		"evaluation": {
			"prompt_version": prompt_spec.version if prompt_spec else None,
			"offline": {
				"dataset": offline_report["dataset"],
				"summary": offline_summary,
			},
			"governed_report": {
				"schema_version": gate_report.get("schema_version"),
				"run_id": gate_report.get("run_id"),
				"mode": gate_report.get("mode"),
				"environment": gate_report.get("environment"),
				"dataset": gate_report.get("dataset"),
				"summary": gate_report.get("summary"),
			} if gate_report else None,
		},
	}


def validate_vector_release(settings: Settings, release: dict) -> dict:
	report, errors = _load_gate_report(
		settings.governance_embedding_gate_report_path,
		expected_schema="myapp-ai-embedding-release-report-v1",
	)
	if report:
		for report_field, release_field in (
			("release_code", "release_code"),
			("embedding_model", "embedding_model"),
			("collection", "collection"),
			("index_version", "index_version"),
		):
			if str(report.get(report_field) or "") != str(release.get(release_field) or ""):
				errors.append(f"The embedding release report {report_field} does not match")
	return {
		"release_gate_eligible": not errors,
		"errors": errors,
		"warnings": [],
		"evaluation": report,
	}
