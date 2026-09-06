from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from .config import Settings
from .prompts import get_prompt_spec
from .release_provenance import prompt_manifest, tool_manifest

VISION_PROBES = (
	(
		"red",
		"data:image/png;base64,"
		"iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAF0lEQVR4nGP4z8BAEiJN9aiG"
		"UQ1DSgMAkPn/Afnh+ngAAAAASUVORK5CYII=",
	),
	(
		"blue",
		"data:image/png;base64,"
		"iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAFUlEQVR4nGNgYPhPIhrVMKph"
		"2GoAAJLb/wFh5Z4RAAAAAElFTkSuQmCC",
	),
)

STRUCTURED_OUTPUT_PROBE_VALUE = "structured-output-ok"
STRUCTURED_OUTPUT_PROBE_SCHEMA = {
	"type": "object",
	"properties": {
		"value": {"type": "string", "enum": [STRUCTURED_OUTPUT_PROBE_VALUE]},
	},
	"required": ["value"],
	"additionalProperties": False,
}


def _provider_error_code(error: Exception) -> str:
	if isinstance(error, httpx.HTTPStatusError):
		return f"PROVIDER_HTTP_{error.response.status_code}"
	if isinstance(error, httpx.TimeoutException):
		return "PROVIDER_TIMEOUT"
	return type(error).__name__.upper()


def _structured_probe_content(body: dict) -> str:
	message = ((body.get("choices") or [{}])[0]).get("message") or {}
	content = str(message.get("content") or "").strip()
	if content.startswith("```"):
		content = content.strip("`").removeprefix("json").strip()
	if not content.startswith("{") and "{" in content and "}" in content:
		content = content[content.find("{") : content.rfind("}") + 1]
	return content


def _validate_structured_probe(body: dict) -> str | None:
	content = _structured_probe_content(body)
	if not content:
		return "STRUCTURED_OUTPUT_EMPTY"
	try:
		value = json.loads(content)
	except json.JSONDecodeError:
		return "STRUCTURED_OUTPUT_INVALID_JSON"
	if value != {"value": STRUCTURED_OUTPUT_PROBE_VALUE}:
		return "STRUCTURED_OUTPUT_SCHEMA_MISMATCH"
	return None


def _probe_structured_output(
	client: httpx.Client, settings: Settings, alias: str,
) -> tuple[bool, bool, str | None]:
	headers = {"Authorization": f"Bearer {settings.litellm_api_key}"}
	base_payload = {
		"model": alias,
		"messages": [{
			"role": "user",
			"content": (
				"Return one JSON object with exactly one key named value whose value is "
				f"{STRUCTURED_OUTPUT_PROBE_VALUE}. Do not use Markdown."
			),
		}],
		"max_completion_tokens": 48,
		"stream": False,
	}
	native_payload = {
		**base_payload,
		"response_format": {
			"type": "json_schema",
			"json_schema": {
				"name": "structured_output_probe",
				"strict": True,
				"schema": STRUCTURED_OUTPUT_PROBE_SCHEMA,
			},
		},
	}
	try:
		response = client.post("/v1/chat/completions", headers=headers, json=native_payload)
		response.raise_for_status()
		error_code = _validate_structured_probe(response.json())
		return error_code is None, error_code is None, error_code
	except httpx.HTTPStatusError as error:
		if error.response.status_code != 400:
			return False, False, _provider_error_code(error)
	except (httpx.HTTPError, ValueError, RuntimeError, TypeError) as error:
		return False, False, _provider_error_code(error)

	fallback_payload = dict(base_payload)
	fallback_payload["messages"] = [{
		"role": "user",
		"content": (
			base_payload["messages"][0]["content"]
			+ " The result must validate against this JSON Schema: "
			+ json.dumps(STRUCTURED_OUTPUT_PROBE_SCHEMA, separators=(",", ":"))
		),
	}]
	try:
		response = client.post("/v1/chat/completions", headers=headers, json=fallback_payload)
		response.raise_for_status()
		error_code = _validate_structured_probe(response.json())
		return error_code is None, False, error_code
	except (httpx.HTTPError, ValueError, RuntimeError, TypeError) as error:
		return False, False, _provider_error_code(error)


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
			"supports_tools": False,
			"supports_json_schema": False,
			"supports_structured_output": False,
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
	mode: str = "full",
) -> dict:
	alias = str(model["model_alias"])
	capability = str(model["capability"])
	started_at = time.monotonic()
	provider_model = None
	error_code = None
	tool_error_code = None
	structured_error_code = None
	vision_error_code = None
	available = False
	supports_tools = False
	supports_json_schema = False
	supports_structured_output = False
	supports_vision = False
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
			if available and capability != "embedding" and mode == "full":
				(
					supports_structured_output,
					supports_json_schema,
					structured_error_code,
				) = _probe_structured_output(client, settings, alias)
				try:
					tool_response = client.post(
						"/v1/chat/completions",
						headers={"Authorization": f"Bearer {settings.litellm_api_key}"},
						json={
							"model": alias,
							"messages": [{"role": "user", "content": "Call capability_probe with value ok."}],
							"max_completion_tokens": 32,
							"stream": False,
							"tools": [{
								"type": "function",
								"function": {
									"name": "capability_probe",
									"description": "Tool-calling capability probe.",
									"parameters": {
										"type": "object",
										"properties": {"value": {"type": "string"}},
										"required": ["value"],
										"additionalProperties": False,
									},
								},
							}],
							"tool_choice": {
								"type": "function", "function": {"name": "capability_probe"},
							},
						},
					)
					tool_response.raise_for_status()
					tool_message = (((tool_response.json().get("choices") or [{}])[0]).get("message") or {})
					tool_calls = tool_message.get("tool_calls") or []
					supports_tools = bool(
						tool_calls
						and ((tool_calls[0].get("function") or {}).get("name") == "capability_probe")
					)
					if not supports_tools:
						tool_error_code = "TOOL_CALL_NOT_RETURNED"
				except httpx.HTTPStatusError as error:
					tool_error_code = f"PROVIDER_HTTP_{error.response.status_code}"
				except httpx.TimeoutException:
					tool_error_code = "PROVIDER_TIMEOUT"
				except (httpx.HTTPError, ValueError, RuntimeError, TypeError) as error:
					tool_error_code = type(error).__name__.upper()
				try:
					supports_vision = True
					for expected_color, image_url in VISION_PROBES:
						vision_response = client.post(
							"/v1/chat/completions",
							headers={"Authorization": f"Bearer {settings.litellm_api_key}"},
							json={
								"model": alias,
								"messages": [{
									"role": "user",
									"content": [
										{
											"type": "text",
											"text": (
												"Identify the single solid color shown in the image. "
												"Reply with exactly one lowercase English color word."
											),
										},
										{"type": "image_url", "image_url": {"url": image_url}},
									],
								}],
								"max_completion_tokens": 12,
								"stream": False,
							},
						)
						vision_response.raise_for_status()
						vision_message = (
							((vision_response.json().get("choices") or [{}])[0]).get("message") or {}
						)
						vision_content = str(vision_message.get("content") or "").strip().casefold()
						if vision_content != expected_color:
							supports_vision = False
							vision_error_code = "VISION_PROBE_MISMATCH"
							break
				except httpx.HTTPStatusError as error:
					supports_vision = False
					vision_error_code = f"PROVIDER_HTTP_{error.response.status_code}"
				except httpx.TimeoutException:
					supports_vision = False
					vision_error_code = "PROVIDER_TIMEOUT"
				except (httpx.HTTPError, ValueError, RuntimeError, TypeError) as error:
					supports_vision = False
					vision_error_code = type(error).__name__.upper()
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
		"supports_tools": supports_tools,
		"tool_error_code": tool_error_code,
		"supports_json_schema": supports_json_schema,
		"supports_structured_output": supports_structured_output,
		"structured_error_code": structured_error_code,
		"supports_vision": supports_vision,
		"vision_error_code": vision_error_code,
	}


def check_model_availability(
	settings: Settings,
	model_aliases: list[str] | None = None,
	transport: httpx.BaseTransport | None = None,
	mode: str = "full",
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
				"supports_tools": False,
				"tool_error_code": None,
				"supports_json_schema": False,
				"supports_structured_output": False,
				"structured_error_code": None,
				"supports_vision": False,
				"vision_error_code": None,
			}
			continue
		probe_models.append(model)

	if probe_models:
		with ThreadPoolExecutor(max_workers=min(4, len(probe_models))) as executor:
			for result in executor.map(
				lambda model: _probe_model(settings, model, transport=transport, mode=mode),
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
	expected_schema: str = "myapp-ai-eval-report-v2",
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


def _report_provenance_errors(report: dict, settings: Settings) -> list[str]:
	errors = []
	provenance = report.get("provenance") or {}
	current_revision = str(settings.runtime_revision or "").strip()
	if not re.fullmatch(r"[0-9a-f]{40,64}", current_revision):
		errors.append("The running Orchestrator revision is not release-grade")
	elif provenance.get("runtime_revision") != current_revision:
		errors.append("The governed report runtime revision does not match")
	if provenance.get("prompt_manifest") != prompt_manifest():
		errors.append("The governed report prompt manifest does not match")
	if provenance.get("tool_manifest") != tool_manifest():
		errors.append("The governed report tool manifest does not match")
	dataset = report.get("dataset") or {}
	if not all(str(dataset.get(field) or "") for field in ("name", "version", "sha256")):
		errors.append("The governed report dataset fingerprint is incomplete")
	return errors


def _report_model_aliases(report: dict) -> list[str]:
	return [
		str(alias)
		for alias in ((report.get("provenance") or {}).get("requested_model_aliases") or [])
		if str(alias)
	]


def _report_dataset_identity(report: dict) -> tuple[str, str, str]:
	dataset = report.get("dataset") or {}
	return (
		str(dataset.get("name") or ""),
		str(dataset.get("version") or ""),
		str(dataset.get("sha256") or ""),
	)


def _report_uses_model(report: dict, model_alias: str) -> bool:
	aliases = {
		str(attempt.get("configured_model_alias") or attempt.get("model_alias") or "")
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

	offline_report, offline_errors = _load_gate_report(
		settings.governance_offline_gate_report_path,
		expected_mode="offline",
	)
	if offline_report:
		offline_errors.extend(_report_provenance_errors(offline_report, settings))
	errors.extend(offline_errors)

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
		if gate_report:
			gate_errors.extend(_report_provenance_errors(gate_report, settings))
			expected_aliases = list(dict.fromkeys([
				primary_model_alias,
				*(str(alias) for alias in (policy.get("fallback_model_aliases") or [])),
			]))
			if _report_model_aliases(gate_report) != expected_aliases:
				gate_errors.append("The live full gate model aliases do not match the policy")
			for alias in expected_aliases:
				if not _report_uses_model(gate_report, alias):
					gate_errors.append(f"The live full gate did not execute model alias {alias}")
			if offline_report and _report_dataset_identity(gate_report) != _report_dataset_identity(
				offline_report
			):
				gate_errors.append("The offline and live full gates do not use the same dataset")
	errors.extend(gate_errors)

	return {
		"release_gate_eligible": not errors,
		"errors": errors,
		"warnings": warnings,
		"evaluation": {
			"prompt_version": prompt_spec.version if prompt_spec else None,
			"offline": {
				"dataset": offline_report.get("dataset"),
				"summary": offline_report.get("summary"),
				"provenance": offline_report.get("provenance"),
			} if offline_report else None,
			"governed_report": {
				"schema_version": gate_report.get("schema_version"),
				"run_id": gate_report.get("run_id"),
				"mode": gate_report.get("mode"),
				"environment": gate_report.get("environment"),
				"dataset": gate_report.get("dataset"),
				"provenance": gate_report.get("provenance"),
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
