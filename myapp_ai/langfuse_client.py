from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import uuid

import httpx

from .config import Settings
from .prompts import get_prompt_spec
from .schemas import ChatRequest, TokenUsage


def _utc_now() -> str:
	return datetime.now(timezone.utc).isoformat()


def _content_summary(value: str) -> dict:
	encoded = value.encode("utf-8")
	return {
		"sha256": hashlib.sha256(encoded).hexdigest(),
		"chars": len(value),
		"bytes": len(encoded),
	}


def _otel_hex_id(value: str, length: int) -> str:
	normalized = str(value or "").replace("-", "").lower()
	if len(normalized) >= length and all(character in "0123456789abcdef" for character in normalized):
		return normalized[:length]
	return hashlib.sha256(str(value or uuid.uuid4()).encode("utf-8")).hexdigest()[:length]


def _unix_nanos(value: str) -> str:
	parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
	if parsed.tzinfo is None:
		parsed = parsed.replace(tzinfo=timezone.utc)
	return str(int(parsed.timestamp() * 1_000_000_000))


def _otel_string(key: str, value) -> dict:
	return {"key": key, "value": {"stringValue": str(value)}}


def _otel_json(key: str, value) -> dict:
	return _otel_string(key, json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _otel_tags(values: list[str]) -> dict:
	return {
		"key": "langfuse.trace.tags",
		"value": {"arrayValue": {"values": [{"stringValue": value} for value in values]}},
	}


class LangfuseClient:
	def __init__(
		self, settings: Settings, transport: httpx.BaseTransport | None = None,
		async_client: httpx.AsyncClient | None = None,
	):
		self.settings = settings
		self.transport = transport
		self.async_client = async_client

	@property
	def enabled(self) -> bool:
		return self.settings.langfuse_enabled

	def _post_batch(self, events: list[dict]) -> bool:
		if not self.enabled:
			return False
		try:
			with httpx.Client(
				base_url=self.settings.langfuse_host,
				timeout=self.settings.langfuse_timeout_seconds,
				transport=self.transport,
				auth=httpx.BasicAuth(
					self.settings.langfuse_public_key,
					self.settings.langfuse_secret_key,
				),
			) as client:
				response = client.post("/api/public/ingestion", json={"batch": events})
				response.raise_for_status()
				body = response.json()
			if not isinstance(body, dict) or body.get("errors"):
				return False
			expected_ids = {str(event.get("id") or "") for event in events}
			success_ids = {
				str(entry.get("id") or "")
				for entry in (body.get("successes") or [])
				if isinstance(entry, dict)
			}
			return bool(expected_ids) and "" not in expected_ids and expected_ids.issubset(success_ids)
		except (httpx.HTTPError, RuntimeError, ValueError, TypeError):
			return False

	async def _apost_batch(self, events: list[dict]) -> bool:
		if not self.enabled or not self.async_client:
			return False
		try:
			response = await self.async_client.post("/api/public/ingestion", json={"batch": events})
			response.raise_for_status()
			body = response.json()
			if not isinstance(body, dict) or body.get("errors"):
				return False
			expected_ids = {str(event.get("id") or "") for event in events}
			success_ids = {
				str(entry.get("id") or "")
				for entry in (body.get("successes") or [])
				if isinstance(entry, dict)
			}
			return bool(expected_ids) and "" not in expected_ids and expected_ids.issubset(success_ids)
		except (httpx.HTTPError, RuntimeError, ValueError, TypeError):
			return False

	def _post_otlp(self, payload: dict) -> bool:
		if not self.enabled:
			return False
		try:
			with httpx.Client(
				base_url=self.settings.langfuse_host,
				timeout=self.settings.langfuse_timeout_seconds,
				transport=self.transport,
				auth=httpx.BasicAuth(
					self.settings.langfuse_public_key,
					self.settings.langfuse_secret_key,
				),
			) as client:
				response = client.post("/api/public/otel/v1/traces", json=payload)
				response.raise_for_status()
			return True
		except (httpx.HTTPError, RuntimeError, ValueError, TypeError):
			return False

	async def _apost_otlp(self, payload: dict) -> bool:
		if not self.enabled or not self.async_client:
			return False
		try:
			response = await self.async_client.post("/api/public/otel/v1/traces", json=payload)
			response.raise_for_status()
			return True
		except (httpx.HTTPError, RuntimeError, ValueError, TypeError):
			return False

	def _trace_metadata(self, request: ChatRequest) -> dict:
		prompt_spec = get_prompt_spec(request.scenario)
		metadata = {
			"scenario": request.scenario,
			"company": request.company,
			"locale": request.locale,
			"prompt_version": prompt_spec.version,
			"prompt_capability": prompt_spec.capability,
			"run_id": request.run_id,
			"conversation_id": request.conversation_id,
			"environment": self.settings.langfuse_environment,
			"release": self.settings.langfuse_release or None,
			"context_tool": (request.context or {}).get("tool") if request.context else None,
			"policy_code": request.policy_code,
			"policy_version": request.policy_version,
			"fallback_reason": request.fallback_reason,
		}
		return {key: value for key, value in metadata.items() if value not in (None, "")}

	def _input(self, request: ChatRequest):
		if self.settings.langfuse_capture_content:
			return [message.model_dump() for message in request.messages]
		return [
			{"role": message.role, "content": _content_summary(message.content)}
			for message in request.messages
		]

	def _output(self, content: str):
		return content if self.settings.langfuse_capture_content else _content_summary(content)

	def _generation_otlp_payload(
		self,
		*,
		request: ChatRequest,
		trace_id: str,
		generation_id: str,
		started_at: str,
		completed_at: str,
		model: str,
		model_alias: str,
		output: str,
		usage: TokenUsage,
		error: str | None,
	) -> dict:
		metadata = self._trace_metadata(request)
		prompt_version = str(metadata["prompt_version"])
		prompt_version_match = re.search(r"-v(\d+)$", prompt_version)
		user_id = hashlib.sha256(f"myapp-ai:{request.user}".encode("utf-8")).hexdigest()
		attributes = [
			_otel_string("langfuse.observation.type", "generation"),
			_otel_string("langfuse.observation.level", "ERROR" if error else "DEFAULT"),
			_otel_json("langfuse.observation.input", self._input(request)),
			_otel_json("langfuse.observation.output", self._output(output)),
			_otel_string("langfuse.observation.model.name", model),
			_otel_json("langfuse.observation.model.parameters", {"model_alias": model_alias}),
			_otel_json("langfuse.observation.usage_details", {
				"input_tokens": usage.prompt_tokens,
				"output_tokens": usage.completion_tokens,
				"total_tokens": usage.total_tokens,
				"reasoning_tokens": usage.reasoning_tokens,
			}),
			_otel_string("langfuse.observation.prompt.name", request.scenario),
			_otel_string("langfuse.trace.name", f"myapp-ai:{request.scenario}"),
			_otel_string("user.id", user_id),
			_otel_string("langfuse.version", prompt_version),
			_otel_tags([request.scenario, prompt_version, self.settings.langfuse_environment]),
		]
		if request.conversation_id:
			attributes.append(_otel_string("session.id", request.conversation_id))
		if prompt_version_match:
			attributes.append({
				"key": "langfuse.observation.prompt.version",
				"value": {"intValue": prompt_version_match.group(1)},
			})
		if error:
			attributes.append(_otel_string("langfuse.observation.status_message", error))
		for key, value in metadata.items():
			attributes.append(_otel_json(f"langfuse.trace.metadata.{key}", value))
			attributes.append(_otel_json(f"langfuse.observation.metadata.{key}", value))

		resource_attributes = [
			_otel_string("service.name", "myapp-ai-orchestrator"),
			_otel_string("telemetry.sdk.language", "python"),
			_otel_string("telemetry.sdk.name", "myapp-ai-otlp"),
			_otel_string("langfuse.environment", self.settings.langfuse_environment),
		]
		if self.settings.langfuse_release:
			resource_attributes.append(_otel_string("langfuse.release", self.settings.langfuse_release))

		return {
			"resourceSpans": [{
				"resource": {"attributes": resource_attributes},
				"scopeSpans": [{
					"scope": {"name": "myapp-ai", "version": "0.1.0"},
					"spans": [{
						"traceId": _otel_hex_id(trace_id, 32),
						"spanId": _otel_hex_id(generation_id, 16),
						"name": "litellm-chat-completion",
						"kind": 3,
						"startTimeUnixNano": _unix_nanos(started_at),
						"endTimeUnixNano": _unix_nanos(completed_at),
						"attributes": attributes,
						"status": {"code": 2 if error else 1, "message": error or ""},
					}],
				}],
			}],
		}

	def record_generation(
		self,
		*,
		request: ChatRequest,
		trace_id: str,
		generation_id: str,
		started_at: str,
		completed_at: str,
		model: str,
		model_alias: str,
		output: str,
		usage: TokenUsage,
		error: str | None = None,
	) -> bool:
		return self._post_otlp(self._generation_otlp_payload(
			request=request, trace_id=trace_id, generation_id=generation_id,
			started_at=started_at, completed_at=completed_at, model=model,
			model_alias=model_alias, output=output, usage=usage, error=error,
		))

	def record_feedback(
		self,
		*,
		trace_id: str,
		run_id: str,
		rating: str,
		category: str | None,
		comment: str | None,
	) -> bool:
		if not trace_id:
			return False
		now = _utc_now()
		metadata = {
			"run_id": run_id,
			"rating": rating,
			"category": category,
		}
		if comment and not self.settings.langfuse_capture_content:
			metadata["comment_summary"] = _content_summary(comment)
		return self._post_batch(
			[
				{
					"id": str(uuid.uuid4()),
					"timestamp": now,
					"type": "score-create",
					"body": {
						"id": str(uuid.uuid4()),
						"traceId": trace_id,
						"name": "user-feedback",
						"value": 1 if rating == "positive" else 0,
						"environment": self.settings.langfuse_environment,
						"source": "API",
						"comment": comment if self.settings.langfuse_capture_content else None,
						"metadata": metadata,
					},
				}
			]
		)

	async def arecord_generation(
		self,
		*,
		request: ChatRequest,
		trace_id: str,
		generation_id: str,
		started_at: str,
		completed_at: str,
		model: str,
		model_alias: str,
		output: str,
		usage: TokenUsage,
		error: str | None = None,
	) -> bool:
		return await self._apost_otlp(self._generation_otlp_payload(
			request=request, trace_id=trace_id, generation_id=generation_id,
			started_at=started_at, completed_at=completed_at, model=model,
			model_alias=model_alias, output=output, usage=usage, error=error,
		))

	async def arecord_feedback(
		self,
		*,
		trace_id: str,
		run_id: str,
		rating: str,
		category: str | None,
		comment: str | None,
	) -> bool:
		if not trace_id:
			return False
		now = _utc_now()
		metadata = {"run_id": run_id, "rating": rating, "category": category}
		if comment and not self.settings.langfuse_capture_content:
			metadata["comment_summary"] = _content_summary(comment)
		return await self._apost_batch([
			{
				"id": str(uuid.uuid4()), "timestamp": now, "type": "score-create",
				"body": {
					"id": str(uuid.uuid4()), "traceId": trace_id, "name": "user-feedback",
					"value": 1 if rating == "positive" else 0,
					"environment": self.settings.langfuse_environment, "source": "API",
					"comment": comment if self.settings.langfuse_capture_content else None,
					"metadata": metadata,
				},
			},
		])

	def record_evaluation_scores(
		self,
		*,
		trace_id: str,
		case_id: str,
		dataset_version: str,
		prompt_version: str,
		mode: str,
		attempt: int,
		scores: dict[str, float],
	) -> bool:
		if not trace_id:
			return False
		now = _utc_now()
		events = []
		for name, value in sorted(scores.items()):
			if not isinstance(value, (int, float)):
				continue
			events.append(
				{
					"id": str(uuid.uuid4()),
					"timestamp": now,
					"type": "score-create",
					"body": {
						"id": str(uuid.uuid4()),
						"traceId": trace_id,
						"name": f"eval.{name}",
						"value": float(value),
						"environment": self.settings.langfuse_environment,
						"source": "EVAL",
						"comment": "Synthetic fixed evaluation; raw content omitted.",
						"metadata": {
							"case_id": case_id,
							"dataset_version": dataset_version,
							"prompt_version": prompt_version,
							"mode": mode,
							"attempt": attempt,
						},
					},
				}
			)
		return self._post_batch(events) if events else False


def utc_now() -> str:
	return _utc_now()
