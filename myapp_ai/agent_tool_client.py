from __future__ import annotations

import asyncio

import httpx

from .config import Settings


class AgentToolClient:
	_RUNTIME_EVENT_MAX_ATTEMPTS = 3
	_RUNTIME_EVENT_RETRY_SECONDS = 0.05
	_TRANSIENT_RUNTIME_EVENT_MARKERS = (
		"querydeadlockerror",
		"record has changed since last read",
		"deadlock found",
		"lock wait timeout",
	)

	def __init__(self, settings: Settings, *, async_client: httpx.AsyncClient):
		self.settings = settings
		self.async_client = async_client

	async def execute(
		self, *, run_id: str, call_id: str, tool: str, arguments: dict, capability_token: str,
	) -> dict:
		response = await self.async_client.post(
			"/api/method/myapp.api.gateway.execute_ai_agent_tool_v1",
			headers={"X-MyApp-AI-Service-Token": self.settings.service_token},
			json={
				"run_id": run_id,
				"call_id": call_id,
				"tool": tool,
				"arguments": arguments,
				"capability_token": capability_token,
			},
		)
		response.raise_for_status()
		body = response.json()
		result = body.get("message", body) if isinstance(body, dict) else None
		if not isinstance(result, dict) or result.get("call_id") != call_id:
			raise RuntimeError("Frappe returned an invalid Agent tool result")
		return result

	async def get_run_control(self, *, run_id: str) -> dict:
		response = await self.async_client.get(
			"/api/method/myapp.api.gateway.get_ai_agent_run_control_v1",
			headers={"X-MyApp-AI-Service-Token": self.settings.service_token},
			params={"run_id": run_id},
		)
		response.raise_for_status()
		body = response.json()
		result = body.get("message", body) if isinstance(body, dict) else None
		if not isinstance(result, dict) or result.get("run_id") != run_id:
			raise RuntimeError("Frappe returned an invalid Agent run-control result")
		return result

	async def record_runtime_event(
		self, *, run_id: str, event_id: str, step_type: str, status: str,
		data: dict, checkpoint: dict | None = None, span_id: str | None = None,
		error_code: str | None = None, capability_token: str,
	) -> dict:
		payload = {
			"run_id": run_id, "event_id": event_id, "step_type": step_type,
			"status": status, "data": data, "checkpoint": checkpoint,
			"span_id": span_id, "error_code": error_code,
			"capability_token": capability_token,
		}
		response = None
		for attempt in range(self._RUNTIME_EVENT_MAX_ATTEMPTS):
			try:
				response = await self.async_client.post(
					"/api/method/myapp.api.gateway.record_ai_agent_runtime_event_v1",
					headers={"X-MyApp-AI-Service-Token": self.settings.service_token},
					json=payload,
				)
			except httpx.TransportError:
				if attempt + 1 >= self._RUNTIME_EVENT_MAX_ATTEMPTS:
					raise
			else:
				if response.is_success or not self._is_transient_runtime_event_response(response):
					break
				if attempt + 1 >= self._RUNTIME_EVENT_MAX_ATTEMPTS:
					break
			await asyncio.sleep(self._RUNTIME_EVENT_RETRY_SECONDS * (attempt + 1))
		if response is None:
			raise RuntimeError("Frappe Agent runtime-event request returned no response")
		response.raise_for_status()
		body = response.json()
		result = body.get("message", body) if isinstance(body, dict) else None
		if not isinstance(result, dict) or result.get("event_id") != event_id:
			raise RuntimeError("Frappe returned an invalid Agent runtime-event result")
		return result

	@classmethod
	def _is_transient_runtime_event_response(cls, response: httpx.Response) -> bool:
		if response.status_code not in {409, 417, 500, 502, 503, 504}:
			return False
		body = response.text.lower()
		return any(marker in body for marker in cls._TRANSIENT_RUNTIME_EVENT_MARKERS)

	async def get_checkpoint(self, *, run_id: str, capability_token: str) -> dict:
		response = await self.async_client.post(
			"/api/method/myapp.api.gateway.get_ai_agent_checkpoint_v1",
			headers={"X-MyApp-AI-Service-Token": self.settings.service_token},
			json={"run_id": run_id, "capability_token": capability_token},
		)
		response.raise_for_status()
		body = response.json()
		result = body.get("message", body) if isinstance(body, dict) else None
		if not isinstance(result, dict) or result.get("run_id") != run_id:
			raise RuntimeError("Frappe returned an invalid Agent checkpoint result")
		return result

	async def request_approval(
		self, *, run_id: str, call_id: str, tool: str, arguments: dict,
		risk_level: str, checkpoint: dict, capability_token: str,
	) -> dict:
		response = await self.async_client.post(
			"/api/method/myapp.api.gateway.request_ai_agent_tool_approval_v1",
			headers={"X-MyApp-AI-Service-Token": self.settings.service_token},
			json={
				"run_id": run_id, "call_id": call_id, "tool": tool,
				"arguments": arguments, "risk_level": risk_level,
				"checkpoint": checkpoint, "capability_token": capability_token,
			},
		)
		response.raise_for_status()
		body = response.json()
		result = body.get("message", body) if isinstance(body, dict) else None
		if not isinstance(result, dict) or result.get("call_id") != call_id:
			raise RuntimeError("Frappe returned an invalid Agent approval result")
		return result
