from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import uuid

import httpx

from .config import Settings
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


class LangfuseClient:
	def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
		self.settings = settings
		self.transport = transport

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
			return True
		except (httpx.HTTPError, RuntimeError, ValueError):
			return False

	def _trace_metadata(self, request: ChatRequest) -> dict:
		metadata = {
			"scenario": request.scenario,
			"company": request.company,
			"locale": request.locale,
			"prompt_version": request.prompt_version,
			"run_id": request.run_id,
			"conversation_id": request.conversation_id,
			"environment": self.settings.langfuse_environment,
			"release": self.settings.langfuse_release or None,
			"context_tool": (request.context or {}).get("tool") if request.context else None,
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
		metadata = self._trace_metadata(request)
		user_id = hashlib.sha256(f"myapp-ai:{request.user}".encode("utf-8")).hexdigest()
		level = "ERROR" if error else "DEFAULT"
		events = [
			{
				"id": str(uuid.uuid4()),
				"timestamp": completed_at,
				"type": "trace-create",
				"body": {
					"id": trace_id,
					"name": f"myapp-ai:{request.scenario}",
					"userId": user_id,
					"sessionId": request.conversation_id,
					"metadata": metadata,
					"tags": [request.scenario, request.prompt_version, self.settings.langfuse_environment],
				},
			},
			{
				"id": str(uuid.uuid4()),
				"timestamp": started_at,
				"type": "generation-create",
				"body": {
					"id": generation_id,
					"traceId": trace_id,
					"name": "litellm-chat-completion",
					"startTime": started_at,
					"model": model,
					"modelParameters": {"model_alias": model_alias},
					"input": self._input(request),
					"metadata": metadata,
				},
			},
			{
				"id": str(uuid.uuid4()),
				"timestamp": completed_at,
				"type": "generation-update",
				"body": {
					"id": generation_id,
					"traceId": trace_id,
					"endTime": completed_at,
					"model": model,
					"output": self._output(output),
					"usage": {
						"input": usage.prompt_tokens,
						"output": usage.completion_tokens,
						"total": usage.total_tokens,
						"unit": "TOKENS",
					},
					"level": level,
					"statusMessage": error,
				},
			},
		]
		return self._post_batch(events)

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
						"comment": comment,
						"metadata": {
							"run_id": run_id,
							"rating": rating,
							"category": category,
						},
					},
				}
			]
		)


def utc_now() -> str:
	return _utc_now()
