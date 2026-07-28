from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import suppress

from .agent_guardrails import (
	AgentRuntimeError,
	check_agent_input,
	check_agent_output,
	sanitize_tool_result,
)
from .agent_tool_client import AgentToolClient
from .agent_tools import tool_approval_policy, tool_definitions, validate_tool_arguments
from .langfuse_client import utc_now
from .litellm_client import LiteLLMClient
from .schemas import AgentCheckpoint, AgentRequest, AgentResponse, AgentStep, ChatMessage, TokenUsage


class AgentRuntime:
	def __init__(self, model_client: LiteLLMClient, tool_client: AgentToolClient):
		self.model_client = model_client
		self.tool_client = tool_client
		self.settings = model_client.settings

	async def _record_span(self, **kwargs) -> None:
		method = getattr(self.model_client.langfuse, "arecord_span", None)
		if method:
			await method(**kwargs)

	async def _checked_guardrail(
		self, *, request: AgentRequest, trace_id: str, parent_span_id: str,
		name: str, checker, input_data: dict,
	):
		started_at = utc_now()
		try:
			result = checker()
		except AgentRuntimeError as error:
			decision_span_id = str(uuid.uuid4())
			await self._record_span(
				request=request, trace_id=trace_id, span_id=decision_span_id,
				parent_span_id=parent_span_id, name=name,
				started_at=started_at, completed_at=utc_now(), input_data=input_data,
				output_data={"status": "blocked", "code": error.code},
				metadata={"run_id": request.run_id}, error=error.code,
			)
			raise
		await self._record_span(
			request=request, trace_id=trace_id, span_id=str(uuid.uuid4()),
			parent_span_id=parent_span_id, name=name,
			started_at=started_at, completed_at=utc_now(), input_data=input_data,
			output_data={"status": result.status}, metadata={"run_id": request.run_id},
		)
		return result

	async def _await_controlled(self, awaitable, *, request: AgentRequest):
		task = asyncio.create_task(awaitable)
		try:
			while True:
				done, _pending = await asyncio.wait(
					{task}, timeout=self.settings.agent_cancel_poll_seconds,
				)
				if task in done:
					return await task
				try:
					control = await self.tool_client.get_run_control(run_id=request.run_id)
				except Exception as error:
					raise AgentRuntimeError(
						"AI_AGENT_CONTROL_UNAVAILABLE",
						"Agent Run 控制状态暂时不可用。",
						retryable=True,
					) from error
				if control.get("cancelled") or control.get("status") != "running":
					raise AgentRuntimeError("AI_RUN_CANCELLED", "用户已取消 AI Run。")
		finally:
			if not task.done():
				task.cancel()
				with suppress(asyncio.CancelledError):
					await task

	async def _controlled_events(self, iterator, *, request: AgentRequest):
		try:
			while True:
				try:
					event = await self._await_controlled(anext(iterator), request=request)
				except StopAsyncIteration:
					break
				yield event
		finally:
			aclose = getattr(iterator, "aclose", None)
			if aclose:
				await aclose()

	def _enforce_token_budget(self, usage: TokenUsage) -> None:
		if usage.total_tokens > self.settings.agent_max_total_tokens:
			raise AgentRuntimeError(
				"AI_AGENT_TOKEN_BUDGET_EXCEEDED",
				"Agent Run 已达到累计 Token 预算。",
			)

	@staticmethod
	def _event_id(step_type: str, steps: list[AgentStep], *, call_id: str | None = None) -> str:
		suffix = call_id or str(len(steps))
		return f"runtime:{step_type}:{suffix}"[:140]

	def _build_checkpoint(
		self, *, request: AgentRequest, stage: str, next_model_step: int,
		messages: list[dict], steps: list[AgentStep], tool_count: int,
		tool_calls: list[dict], pending_tool_calls: list[dict], tool_results: list[dict], citations: list[dict],
		usage: TokenUsage, model: str, trace_id: str, agent_span_id: str,
		final_content: str | None = None,
		pending_approval: dict | None = None,
	) -> AgentCheckpoint:
		return AgentCheckpoint(
			run_id=request.run_id, stage=stage, next_model_step=next_model_step,
			tool_count=tool_count, runtime_messages=messages[1:], agent_steps=steps,
			tool_calls=tool_calls, pending_tool_calls=pending_tool_calls,
			pending_approval=pending_approval,
			tool_results=tool_results, citations=citations,
			usage=usage, model=model, model_alias=self.settings.model,
			prompt_version=str(request.prompt_version or ""),
			trace_id=trace_id, agent_span_id=agent_span_id,
			final_content=final_content,
		)

	async def _persist_event(
		self, *, request: AgentRequest, event_id: str, step_type: str,
		status: str = "completed", data: dict | None = None,
		checkpoint: AgentCheckpoint | None = None, span_id: str | None = None,
		error_code: str | None = None,
	) -> None:
		try:
			await self.tool_client.record_runtime_event(
				run_id=request.run_id, event_id=event_id, step_type=step_type,
				status=status, data=data or {},
				checkpoint=checkpoint.model_dump(mode="json") if checkpoint else None,
				span_id=span_id, error_code=error_code,
				capability_token=request.capability_token,
			)
		except Exception as error:
			raise AgentRuntimeError(
				"AI_AGENT_CHECKPOINT_UNAVAILABLE",
				"Agent 运行检查点暂时无法持久化。",
				retryable=True,
			) from error

	async def _persist_guardrail_failure(
		self, *, request: AgentRequest, phase: str, error: AgentRuntimeError,
	) -> None:
		await self._persist_event(
			request=request, event_id=f"runtime:{phase}:failed:{uuid.uuid4().hex}"[:140],
			step_type=f"{phase}_guardrail", status="failed",
			data={"phase": phase, "status": "blocked", "code": error.code},
			error_code=error.code,
		)

	@staticmethod
	def _merge_usage(total: TokenUsage, current: TokenUsage) -> TokenUsage:
		return TokenUsage(
			prompt_tokens=total.prompt_tokens + current.prompt_tokens,
			completion_tokens=total.completion_tokens + current.completion_tokens,
			total_tokens=total.total_tokens + current.total_tokens,
			reasoning_tokens=total.reasoning_tokens + current.reasoning_tokens,
		)

	def _initial_payload(self, request: AgentRequest) -> tuple[dict, str, AgentRequest]:
		# Agent business data must arrive only through tool messages. Ignore any
		# legacy context field on this endpoint instead of elevating it to system.
		request = request.model_copy(update={"context": None})
		payload, trace_id, normalized = self.model_client._build_payload(request)
		payload["tools"] = tool_definitions(request.allowed_tools)
		payload["tool_choice"] = "auto"
		return payload, trace_id, normalized

	async def _model_call(
		self, *, payload: dict, request: AgentRequest, trace_id: str,
	) -> tuple[dict, TokenUsage, str, int]:
		generation_id = str(uuid.uuid4())
		started_at = utc_now()
		started = time.perf_counter()
		try:
			self.model_client._fit_payload_context(payload)
			body = await self._await_controlled(
				self.model_client._apost_chat(payload, trace_id),
				request=request,
			)
		except Exception as error:
			await self.model_client._arecord_generation(
				request=request, trace_id=trace_id, generation_id=generation_id,
				started_at=started_at, completed_at=utc_now(), model=self.settings.model,
				model_alias=self.settings.model, output="", usage=TokenUsage(),
				error=type(error).__name__,
			)
			raise
		usage = self.model_client._usage(body.get("usage") or {})
		model = str(body.get("model") or self.settings.model)
		message = ((body.get("choices") or [{}])[0].get("message") or {})
		await self.model_client._arecord_generation(
			request=request, trace_id=trace_id, generation_id=generation_id,
			started_at=started_at, completed_at=utc_now(), model=model,
			model_alias=self.settings.model,
			output=str(message.get("content") or ""), usage=usage,
		)
		return message, usage, model, int((time.perf_counter() - started) * 1000)

	async def _stream_grounded_answer(
		self, *, payload: dict, request: AgentRequest, trace_id: str, parent_span_id: str,
	):
		if not self.model_client.async_client:
			raise RuntimeError("Shared LiteLLM AsyncClient is not configured")
		generation_id = str(uuid.uuid4())
		started_at = utc_now()
		started = time.perf_counter()
		first_token_ms = None
		content_parts: list[str] = []
		usage = TokenUsage()
		model = self.settings.model
		stream_payload = self.model_client._fit_payload_context({
			**payload,
			"stream": True,
			"stream_options": {"include_usage": True},
			"tool_choice": "none",
		})
		try:
			async with self.model_client.async_client.stream(
				"POST",
				"/v1/chat/completions",
				headers={
					"Authorization": f"Bearer {self.settings.litellm_api_key}",
					"Content-Type": "application/json",
					"X-MyApp-Trace-Id": trace_id,
				},
				json=stream_payload,
			) as response:
				response.raise_for_status()
				async for line in response.aiter_lines():
					if not line or line.startswith(":") or not line.startswith("data:"):
						continue
					data = line[5:].strip()
					if data == "[DONE]":
						break
					chunk = json.loads(data)
					model = str(chunk.get("model") or model)
					if chunk.get("usage"):
						usage = self.model_client._usage(chunk["usage"])
					choice = (chunk.get("choices") or [{}])[0]
					delta = str((choice.get("delta") or {}).get("content") or "")
					if delta:
						check_agent_output("".join(content_parts[-8:]) + delta)
						if first_token_ms is None:
							first_token_ms = int((time.perf_counter() - started) * 1000)
						content_parts.append(delta)
						yield {"type": "message_delta", "delta": delta}
		except Exception as error:
			if isinstance(error, AgentRuntimeError):
				await self._record_span(
					request=request, trace_id=trace_id, span_id=str(uuid.uuid4()),
					parent_span_id=parent_span_id, name="agent.output_guardrail",
					started_at=started_at, completed_at=utc_now(),
					input_data={"streaming": True},
					output_data={"status": "blocked", "code": error.code},
					metadata={"run_id": request.run_id}, error=error.code,
				)
			await self.model_client._arecord_generation(
				request=request, trace_id=trace_id, generation_id=generation_id,
				started_at=started_at, completed_at=utc_now(), model=model,
				model_alias=self.settings.model, output="".join(content_parts), usage=usage,
				error=type(error).__name__,
			)
			raise
		content = "".join(content_parts).strip()
		if not content:
			await self.model_client._arecord_generation(
				request=request, trace_id=trace_id, generation_id=generation_id,
				started_at=started_at, completed_at=utc_now(), model=model,
				model_alias=self.settings.model, output="", usage=usage,
				error="EmptyModelResponse",
			)
			raise RuntimeError("Agent model returned an empty grounded stream")
		await self.model_client._arecord_generation(
			request=request, trace_id=trace_id, generation_id=generation_id,
			started_at=started_at, completed_at=utc_now(), model=model,
			model_alias=self.settings.model, output=content, usage=usage,
		)
		yield {
			"type": "grounded_completed",
			"content": content,
			"usage": usage,
			"model": model,
			"latency_ms": int((time.perf_counter() - started) * 1000),
			"first_token_ms": first_token_ms,
		}

	@staticmethod
	def _parse_tool_calls(message: dict, allowed_tools: list[str]) -> list[dict]:
		result = []
		for raw in message.get("tool_calls") or []:
			function = raw.get("function") or {}
			name = str(function.get("name") or "").strip()
			call_id = str(raw.get("id") or "").strip()
			if not call_id or name not in allowed_tools:
				raise AgentRuntimeError("AI_AGENT_TOOL_UNAUTHORIZED", "模型请求了未授权的 Agent 工具。")
			try:
				arguments = json.loads(function.get("arguments") or "{}")
			except (TypeError, ValueError) as error:
				raise AgentRuntimeError(
					"AI_AGENT_TOOL_ARGUMENTS_INVALID", "模型返回了无效的 Agent 工具参数。",
				) from error
			if not isinstance(arguments, dict):
				raise AgentRuntimeError(
					"AI_AGENT_TOOL_ARGUMENTS_INVALID", "Agent 工具参数必须是对象。",
				)
			arguments = validate_tool_arguments(name, arguments)
			result.append({
				"id": call_id,
				"type": "function",
				"function": {
					"name": name,
					"arguments": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
				},
				"arguments": arguments,
			})
		return result

	@staticmethod
	def _tool_message(result: dict) -> dict:
		content = json.dumps(
			{
				"status": result.get("status"),
				"data": result.get("model_context") or {},
				"error": result.get("error"),
				"retryable": bool(result.get("retryable")),
			},
			ensure_ascii=False,
			separators=(",", ":"),
		)
		if len(content.encode("utf-8")) > 60000:
			raise RuntimeError("Agent tool result exceeds the model context boundary")
		return {
			"role": "tool",
			"tool_call_id": result["call_id"],
			"name": result["tool"],
			"content": content,
		}

	@staticmethod
	def _dedupe_citations(citations: list[dict]) -> list[dict]:
		result = []
		seen = set()
		for citation in citations:
			key = (str(citation.get("type") or ""), str(citation.get("id") or ""))
			if key in seen:
				continue
			seen.add(key)
			result.append(citation)
		return result

	async def _execute_tool_call(
		self, *, request: AgentRequest, call: dict, model_step: int,
		remaining_calls: list[dict], messages: list[dict], steps: list[AgentStep],
		tool_count: int, tool_calls_audit: list[dict], tool_results: list[dict],
		citations: list[dict], total_usage: TokenUsage, model: str,
		trace_id: str, agent_span_id: str,
	) -> tuple[int, dict, int]:
		tool_count += 1
		if tool_count > self.settings.agent_max_tool_calls:
			raise AgentRuntimeError(
				"AI_AGENT_TOOL_BUDGET_EXCEEDED", "Agent Run 已达到工具调用次数上限。",
			)
		tool_started_at = utc_now()
		started = time.perf_counter()
		result = await self._await_controlled(
			self.tool_client.execute(
				run_id=request.run_id, call_id=call["id"],
				tool=call["function"]["name"], arguments=call["arguments"],
				capability_token=request.capability_token,
			),
			request=request,
		)
		result, tool_guardrail = sanitize_tool_result(result)
		latency = int((time.perf_counter() - started) * 1000)
		tool_guardrail_span_id = str(uuid.uuid4())
		await self._record_span(
			request=request, trace_id=trace_id, span_id=tool_guardrail_span_id,
			parent_span_id=agent_span_id, name="agent.tool_guardrail",
			started_at=tool_started_at, completed_at=utc_now(),
			input_data={"call_id": call["id"], "tool": call["function"]["name"]},
			output_data={
				"status": tool_guardrail.status,
				"sanitized_fields": tool_guardrail.sanitized_fields,
			},
			metadata={"run_id": request.run_id, "call_id": call["id"]},
		)
		await self._persist_event(
			request=request, event_id=f"runtime:tool_guardrail:{call['id']}"[:140],
			step_type="tool_guardrail", span_id=tool_guardrail_span_id,
			data={
				"call_id": call["id"], "tool": result.get("tool"),
				"status": tool_guardrail.status,
				"sanitized_fields": tool_guardrail.sanitized_fields,
			},
		)
		steps.append(AgentStep(
			step_no=len(steps) + 1, type="guardrail", status="completed",
			call_id=call["id"], tool=result.get("tool"),
			guardrail_phase=tool_guardrail.phase,
		))
		await self._record_span(
			request=request, trace_id=trace_id, span_id=str(uuid.uuid4()),
			parent_span_id=agent_span_id, name=f"agent-tool:{result.get('tool')}",
			started_at=tool_started_at, completed_at=utc_now(),
			input_data={
				"call_id": call["id"], "tool": call["function"]["name"],
				"arguments": call["arguments"],
			},
			output_data={
				"status": result.get("status"),
				"result_count": len(result.get("citations") or []),
			},
			metadata={"call_id": call["id"], "tool": result.get("tool")},
			error=(result.get("error") or {}).get("code"),
		)
		steps.append(AgentStep(
			step_no=len(steps) + 1, type="tool", status="completed",
			call_id=call["id"], tool=result.get("tool"),
			result_status=result.get("status"), latency_ms=latency,
			error_code=(result.get("error") or {}).get("code"),
		))
		tool_calls_audit.append({
			"call_id": call["id"], "tool": result.get("tool"),
			"arguments": call["arguments"], "status": result.get("status"),
			"result_count": len(result.get("citations") or []),
		})
		tool_results.append(result)
		citations.extend(result.get("citations") or [])
		messages.append(self._tool_message(result))
		tool_checkpoint = self._build_checkpoint(
			request=request, stage="tool_completed", next_model_step=model_step + 1,
			messages=messages, steps=steps, tool_count=tool_count,
			tool_calls=tool_calls_audit, pending_tool_calls=remaining_calls,
			tool_results=tool_results, citations=citations,
			usage=total_usage, model=model, trace_id=trace_id, agent_span_id=agent_span_id,
		)
		await self._persist_event(
			request=request,
			event_id=self._event_id("checkpoint", steps, call_id=call["id"]),
			step_type="checkpoint", data={
				"stage": "tool_completed", "model_step": model_step,
				"call_id": call["id"], "tool": result.get("tool"),
				"remaining_tool_calls": len(remaining_calls),
			}, checkpoint=tool_checkpoint,
		)
		return tool_count, result, latency

	async def _request_approval_if_needed(
		self, *, request: AgentRequest, call: dict, model_step: int,
		remaining_calls: list[dict], messages: list[dict], steps: list[AgentStep],
		tool_count: int, tool_calls_audit: list[dict], tool_results: list[dict],
		citations: list[dict], total_usage: TokenUsage, model: str,
		trace_id: str, agent_span_id: str,
	) -> dict | None:
		tool = call["function"]["name"]
		policy = tool_approval_policy(tool)
		if not policy.get("required"):
			return None
		resume_approval = request.approval or {}
		if (
			str(resume_approval.get("call_id") or "") == call["id"]
			and str(resume_approval.get("tool") or "") == tool
			and str(resume_approval.get("status") or "") in {"approved", "rejected"}
		):
			return None
		approval_context = {
			"call_id": call["id"], "tool": tool,
			"risk_level": str(policy.get("risk_level") or "L3_SENSITIVE"),
		}
		checkpoint = self._build_checkpoint(
			request=request, stage="waiting_approval", next_model_step=model_step + 1,
			messages=messages, steps=steps, tool_count=tool_count,
			tool_calls=tool_calls_audit, pending_tool_calls=[call, *remaining_calls],
			pending_approval=approval_context,
			tool_results=tool_results, citations=citations, usage=total_usage,
			model=model, trace_id=trace_id, agent_span_id=agent_span_id,
		)
		try:
			approval = await self.tool_client.request_approval(
				run_id=request.run_id, call_id=call["id"], tool=tool,
				arguments=call["arguments"], risk_level=approval_context["risk_level"],
				checkpoint=checkpoint.model_dump(mode="json"),
				capability_token=request.capability_token,
			)
		except Exception as error:
			raise AgentRuntimeError(
				"AI_AGENT_APPROVAL_UNAVAILABLE",
				"Agent 工具审批状态暂时无法持久化。",
				retryable=True,
			) from error
		return approval if approval.get("status") == "pending" else None

	def _paused_response(
		self, *, request: AgentRequest, approval: dict, model: str, trace_id: str,
		usage: TokenUsage, steps: list[AgentStep], tool_calls: list[dict],
		tool_results: list[dict], citations: list[dict],
	) -> AgentResponse:
		return AgentResponse(
			status="waiting_approval",
			message=ChatMessage(role="assistant", content="该工具调用需要人工审批后才能继续。"),
			model=model, model_alias=self.settings.model, trace_id=trace_id,
			usage=usage, warnings=self.model_client._warnings(request),
			agent_steps=steps, tool_calls=tool_calls, tool_results=tool_results,
			citations=self._dedupe_citations(citations), approval=approval,
		)

	async def run(self, request: AgentRequest) -> AgentResponse:
		try:
			async with asyncio.timeout(self.settings.agent_run_timeout_seconds):
				return await self._run(request)
		except TimeoutError as error:
			raise AgentRuntimeError(
				"AI_AGENT_DEADLINE_EXCEEDED", "Agent Run 已超过统一执行时限。", retryable=True,
			) from error

	async def _load_checkpoint(self, request: AgentRequest) -> AgentCheckpoint:
		try:
			state = await self.tool_client.get_checkpoint(
				run_id=request.run_id, capability_token=request.capability_token,
			)
			checkpoint = AgentCheckpoint.model_validate(state.get("checkpoint"))
		except Exception as error:
			raise AgentRuntimeError(
				"AI_AGENT_CHECKPOINT_INVALID", "Agent Run 没有可恢复的有效检查点。",
			) from error
		if checkpoint.run_id != request.run_id:
			raise AgentRuntimeError("AI_AGENT_CHECKPOINT_INVALID", "Agent 检查点与 Run 不匹配。")
		if checkpoint.prompt_version and checkpoint.prompt_version != str(request.prompt_version or ""):
			raise AgentRuntimeError("AI_AGENT_CHECKPOINT_INVALID", "Agent 检查点 Prompt 版本不匹配。")
		if checkpoint.model_alias and checkpoint.model_alias != str(request.model_alias or self.settings.model):
			raise AgentRuntimeError("AI_AGENT_CHECKPOINT_INVALID", "Agent 检查点模型别名不匹配。")
		normalized_pending_calls = []
		for raw in checkpoint.pending_tool_calls:
			try:
				call_id = str(raw["id"]).strip()
				function = raw["function"]
				name = str(function["name"]).strip()
				arguments = validate_tool_arguments(name, raw["arguments"])
			except (KeyError, TypeError, ValueError, AgentRuntimeError) as error:
				raise AgentRuntimeError(
					"AI_AGENT_CHECKPOINT_INVALID", "Agent 检查点包含无效的待执行工具调用。",
				) from error
			if not call_id or name not in request.allowed_tools:
				raise AgentRuntimeError(
					"AI_AGENT_CHECKPOINT_INVALID", "Agent 检查点包含未授权的待执行工具调用。",
				)
			normalized_pending_calls.append({
				"id": call_id, "type": "function",
				"function": {
					"name": name,
					"arguments": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
				},
				"arguments": arguments,
			})
		if checkpoint.stage == "waiting_approval":
			pending_approval = checkpoint.pending_approval or {}
			if not normalized_pending_calls:
				raise AgentRuntimeError(
					"AI_AGENT_CHECKPOINT_INVALID", "Agent 待审批检查点缺少工具调用。",
				)
			first_call = normalized_pending_calls[0]
			if (
				str(pending_approval.get("call_id") or "") != first_call["id"]
				or str(pending_approval.get("tool") or "") != first_call["function"]["name"]
			):
				raise AgentRuntimeError(
					"AI_AGENT_CHECKPOINT_INVALID", "Agent 待审批检查点绑定不一致。",
				)
			if request.approval:
				if (
					str(request.approval.get("approval_id") or "")
					!= str(pending_approval.get("approval_id") or "")
					or str(request.approval.get("call_id") or "") != first_call["id"]
					or str(request.approval.get("tool") or "") != first_call["function"]["name"]
					or str(request.approval.get("status") or "") not in {"approved", "rejected"}
				):
					raise AgentRuntimeError(
						"AI_AGENT_CHECKPOINT_INVALID", "Agent 审批决定与检查点不一致。",
					)
		return checkpoint.model_copy(update={"pending_tool_calls": normalized_pending_calls})

	async def resume(self, request: AgentRequest) -> AgentResponse:
		checkpoint = await self._load_checkpoint(request)
		try:
			async with asyncio.timeout(self.settings.agent_run_timeout_seconds):
				return await self._run(request, checkpoint=checkpoint)
		except TimeoutError as error:
			raise AgentRuntimeError(
				"AI_AGENT_DEADLINE_EXCEEDED", "Agent Run 已超过统一执行时限。", retryable=True,
			) from error

	async def _run(
		self, request: AgentRequest, *, checkpoint: AgentCheckpoint | None = None,
	) -> AgentResponse:
		payload, trace_id, request = self._initial_payload(request)
		agent_span_id = checkpoint.agent_span_id if checkpoint else str(uuid.uuid4())
		if checkpoint:
			trace_id = checkpoint.trace_id
		agent_started_at = utc_now()
		if checkpoint:
			messages = [payload["messages"][0], *checkpoint.runtime_messages]
			steps = list(checkpoint.agent_steps)
			tool_calls_audit = list(checkpoint.tool_calls)
			tool_results = list(checkpoint.tool_results)
			citations = list(checkpoint.citations)
			total_usage = checkpoint.usage
			model = checkpoint.model or self.settings.model
			tool_count = checkpoint.tool_count
			start_model_step = checkpoint.next_model_step
			pending_calls = list(checkpoint.pending_tool_calls)
			if checkpoint.stage == "output_guardrail" and checkpoint.final_content:
				return AgentResponse(
					message=ChatMessage(role="assistant", content=checkpoint.final_content),
					model=model, model_alias=self.settings.model, trace_id=trace_id,
					usage=total_usage, warnings=self.model_client._warnings(request),
					agent_steps=steps, tool_calls=tool_calls_audit,
					tool_results=tool_results, citations=self._dedupe_citations(citations),
				)
		else:
			messages = list(payload["messages"])
			steps: list[AgentStep] = []
			tool_calls_audit: list[dict] = []
			tool_results: list[dict] = []
			citations: list[dict] = []
			total_usage = TokenUsage()
			model = self.settings.model
			tool_count = 0
			start_model_step = 1
			pending_calls = []
			try:
				input_guardrail = await self._checked_guardrail(
					request=request, trace_id=trace_id, parent_span_id=agent_span_id,
					name="agent.input_guardrail", checker=lambda: check_agent_input(request),
					input_data={"message_count": len(request.messages)},
				)
			except AgentRuntimeError as error:
				await self._persist_guardrail_failure(request=request, phase="input", error=error)
				raise
			steps.append(AgentStep(
				step_no=1, type="guardrail", status="completed", guardrail_phase=input_guardrail.phase,
			))
			input_checkpoint = self._build_checkpoint(
				request=request, stage="input_guardrail", next_model_step=1,
				messages=messages, steps=steps, tool_count=tool_count,
				tool_calls=tool_calls_audit, pending_tool_calls=[],
				tool_results=tool_results, citations=citations,
				usage=total_usage, model=model, trace_id=trace_id, agent_span_id=agent_span_id,
			)
			await self._persist_event(
				request=request, event_id=self._event_id("input_guardrail", steps),
				step_type="input_guardrail", data={"status": input_guardrail.status},
				checkpoint=input_checkpoint,
			)

		if pending_calls:
			pending_model_step = max(1, start_model_step - 1)
			for index, call in enumerate(pending_calls):
				approval = await self._request_approval_if_needed(
					request=request, call=call, model_step=pending_model_step,
					remaining_calls=pending_calls[index + 1:], messages=messages,
					steps=steps, tool_count=tool_count, tool_calls_audit=tool_calls_audit,
					tool_results=tool_results, citations=citations, total_usage=total_usage,
					model=model, trace_id=trace_id, agent_span_id=agent_span_id,
				)
				if approval:
					return self._paused_response(
						request=request, approval=approval, model=model, trace_id=trace_id,
						usage=total_usage, steps=steps, tool_calls=tool_calls_audit,
						tool_results=tool_results, citations=citations,
					)
				tool_count, _result, _latency = await self._execute_tool_call(
					request=request, call=call, model_step=pending_model_step,
					remaining_calls=pending_calls[index + 1:], messages=messages,
					steps=steps, tool_count=tool_count, tool_calls_audit=tool_calls_audit,
					tool_results=tool_results, citations=citations, total_usage=total_usage,
					model=model, trace_id=trace_id, agent_span_id=agent_span_id,
				)

		for step_no in range(start_model_step, self.settings.agent_max_steps + 1):
			payload["messages"] = messages
			decision_started_at = utc_now()
			message, usage, model, latency_ms = await self._model_call(
				payload=payload, request=request, trace_id=trace_id,
			)
			total_usage = self._merge_usage(total_usage, usage)
			self._enforce_token_budget(total_usage)
			steps.append(AgentStep(step_no=len(steps) + 1, type="model", status="completed", latency_ms=latency_ms))
			calls = self._parse_tool_calls(message, request.allowed_tools)
			decision_span_id = str(uuid.uuid4())
			await self._record_span(
				request=request, trace_id=trace_id, span_id=decision_span_id,
				parent_span_id=agent_span_id, name="agent.model_decision",
				started_at=decision_started_at, completed_at=utc_now(),
				input_data={"step_no": step_no, "available_tools": request.allowed_tools},
				output_data={
					"decision": "tool_calls" if calls else "final_answer",
					"tool_call_count": len(calls),
				}, metadata={"run_id": request.run_id, "step_no": step_no},
			)
			decision_checkpoint = None
			if calls:
				messages.append({
					"role": "assistant",
					"content": message.get("content"),
					"tool_calls": [
						{"id": call["id"], "type": "function", "function": call["function"]}
						for call in calls
					],
				})
				decision_checkpoint = self._build_checkpoint(
					request=request, stage="model_decision", next_model_step=step_no + 1,
					messages=messages, steps=steps, tool_count=tool_count,
					tool_calls=tool_calls_audit, pending_tool_calls=calls,
					tool_results=tool_results, citations=citations,
					usage=total_usage, model=model, trace_id=trace_id,
					agent_span_id=agent_span_id,
				)
			await self._persist_event(
				request=request, event_id=f"runtime:model_decision:{step_no}",
				step_type="model_decision", span_id=decision_span_id,
				data={
					"model_step": step_no,
					"decision": "tool_calls" if calls else "final_answer",
					"tools": [call["function"]["name"] for call in calls],
					"latency_ms": latency_ms,
				}, checkpoint=decision_checkpoint,
			)
			if not calls:
				content = str(message.get("content") or "").strip()
				if not content:
					raise RuntimeError("Agent model returned neither tool calls nor a final answer")
				try:
					output_guardrail = await self._checked_guardrail(
						request=request, trace_id=trace_id, parent_span_id=agent_span_id,
						name="agent.output_guardrail", checker=lambda: check_agent_output(content),
						input_data={"has_tool_results": bool(tool_results)},
					)
				except AgentRuntimeError as error:
					await self._persist_guardrail_failure(request=request, phase="output", error=error)
					raise
				steps.append(AgentStep(
					step_no=len(steps) + 1, type="guardrail", status="completed",
					guardrail_phase=output_guardrail.phase,
				))
				output_checkpoint = self._build_checkpoint(
					request=request, stage="output_guardrail", next_model_step=step_no + 1,
					messages=messages, steps=steps, tool_count=tool_count,
					tool_calls=tool_calls_audit, pending_tool_calls=[],
					tool_results=tool_results, citations=citations,
					usage=total_usage, model=model, trace_id=trace_id, agent_span_id=agent_span_id,
					final_content=content,
				)
				await self._persist_event(
					request=request, event_id=self._event_id("output_guardrail", steps),
					step_type="output_guardrail", data={"status": output_guardrail.status},
					checkpoint=output_checkpoint,
				)
				response = AgentResponse(
					message=ChatMessage(role="assistant", content=content),
					model=model, model_alias=self.settings.model, trace_id=trace_id,
					usage=total_usage, warnings=self.model_client._warnings(request),
					agent_steps=steps, tool_calls=tool_calls_audit,
					tool_results=tool_results, citations=self._dedupe_citations(citations),
				)
				await self._record_span(
					request=request, trace_id=trace_id, span_id=agent_span_id,
					name="agent-run", started_at=agent_started_at, completed_at=utc_now(),
					input_data={"allowed_tools": request.allowed_tools},
					output_data={"steps": len(steps), "tool_calls": tool_count, "status": "completed"},
					metadata={"run_id": request.run_id},
				)
				return response

			for index, call in enumerate(calls):
				approval = await self._request_approval_if_needed(
					request=request, call=call, model_step=step_no,
					remaining_calls=calls[index + 1:], messages=messages,
					steps=steps, tool_count=tool_count, tool_calls_audit=tool_calls_audit,
					tool_results=tool_results, citations=citations, total_usage=total_usage,
					model=model, trace_id=trace_id, agent_span_id=agent_span_id,
				)
				if approval:
					return self._paused_response(
						request=request, approval=approval, model=model, trace_id=trace_id,
						usage=total_usage, steps=steps, tool_calls=tool_calls_audit,
						tool_results=tool_results, citations=citations,
					)
				tool_count, _result, _latency = await self._execute_tool_call(
					request=request, call=call, model_step=step_no,
					remaining_calls=calls[index + 1:], messages=messages, steps=steps,
					tool_count=tool_count, tool_calls_audit=tool_calls_audit,
					tool_results=tool_results, citations=citations, total_usage=total_usage,
					model=model, trace_id=trace_id, agent_span_id=agent_span_id,
				)

		raise AgentRuntimeError(
			"AI_AGENT_STEP_BUDGET_EXCEEDED", "Agent Run 已达到模型步骤上限，仍未形成最终回答。",
		)

	async def stream(self, request: AgentRequest):
		try:
			async with asyncio.timeout(self.settings.agent_run_timeout_seconds):
				async for event in self._stream(request):
					yield event
		except TimeoutError as error:
			raise AgentRuntimeError(
				"AI_AGENT_DEADLINE_EXCEEDED", "Agent Run 已超过统一执行时限。", retryable=True,
			) from error

	async def resume_stream(self, request: AgentRequest):
		checkpoint = await self._load_checkpoint(request)
		try:
			async with asyncio.timeout(self.settings.agent_run_timeout_seconds):
				async for event in self._stream(request, checkpoint=checkpoint):
					yield event
		except TimeoutError as error:
			raise AgentRuntimeError(
				"AI_AGENT_DEADLINE_EXCEEDED", "Agent Run 已超过统一执行时限。", retryable=True,
			) from error

	async def _stream(
		self, request: AgentRequest, *, checkpoint: AgentCheckpoint | None = None,
	):
		# Tool decisions are bounded non-streaming calls. Once a tool returns a
		# terminal business result, the final grounded answer uses the provider's
		# real SSE stream with tool_choice=none.
		payload, trace_id, request = self._initial_payload(request)
		agent_span_id = checkpoint.agent_span_id if checkpoint else str(uuid.uuid4())
		if checkpoint:
			trace_id = checkpoint.trace_id
		agent_started_at = utc_now()
		if checkpoint:
			messages = [payload["messages"][0], *checkpoint.runtime_messages]
			steps = list(checkpoint.agent_steps)
			tool_calls_audit = list(checkpoint.tool_calls)
			tool_results = list(checkpoint.tool_results)
			citations = list(checkpoint.citations)
			total_usage = checkpoint.usage
			model = checkpoint.model or self.settings.model
			tool_count = checkpoint.tool_count
			start_model_step = checkpoint.next_model_step
			pending_calls = list(checkpoint.pending_tool_calls)
		else:
			messages = list(payload["messages"])
			steps = []
			tool_calls_audit = []
			tool_results = []
			citations = []
			total_usage = TokenUsage()
			model = self.settings.model
			tool_count = 0
			start_model_step = 1
			pending_calls = []
		first_token_ms = None
		output_guardrail = None
		output_checkpoint_saved = False
		final_next_model_step = start_model_step
		yield {
			"type": "started", "trace_id": trace_id, "model_alias": self.settings.model,
			"resumed": bool(checkpoint),
		}
		if checkpoint and checkpoint.stage == "output_guardrail" and checkpoint.final_content:
			content = checkpoint.final_content
			yield {"type": "message_delta", "delta": content, "replayed": True}
			for warning in self.model_client._warnings(request):
				yield {"type": "warning", "message": warning}
			yield {
				"type": "completed", "message": {"role": "assistant", "content": content},
				"model": model, "model_alias": self.settings.model, "trace_id": trace_id,
				"usage": total_usage.model_dump(), "warnings": self.model_client._warnings(request),
				"first_token_ms": 0, "resumed": True, "replayed": True,
				"agent_steps": [step.model_dump() for step in steps],
				"tool_calls": tool_calls_audit, "tool_results": tool_results,
				"citations": self._dedupe_citations(citations),
			}
			return
		if not checkpoint:
			try:
				input_guardrail = await self._checked_guardrail(
					request=request, trace_id=trace_id, parent_span_id=agent_span_id,
					name="agent.input_guardrail", checker=lambda: check_agent_input(request),
					input_data={"message_count": len(request.messages)},
				)
			except AgentRuntimeError as error:
				await self._persist_guardrail_failure(request=request, phase="input", error=error)
				raise
			steps.append(AgentStep(
				step_no=1, type="guardrail", status="completed", guardrail_phase=input_guardrail.phase,
			))
			input_checkpoint = self._build_checkpoint(
				request=request, stage="input_guardrail", next_model_step=1,
				messages=messages, steps=steps, tool_count=tool_count,
				tool_calls=tool_calls_audit, pending_tool_calls=[], tool_results=tool_results,
				citations=citations, usage=total_usage, model=model, trace_id=trace_id,
				agent_span_id=agent_span_id,
			)
			await self._persist_event(
				request=request, event_id=self._event_id("input_guardrail", steps),
				step_type="input_guardrail", data={"status": input_guardrail.status},
				checkpoint=input_checkpoint,
			)
		if pending_calls:
			pending_model_step = max(1, start_model_step - 1)
			for index, call in enumerate(pending_calls):
				approval = await self._request_approval_if_needed(
					request=request, call=call, model_step=pending_model_step,
					remaining_calls=pending_calls[index + 1:], messages=messages,
					steps=steps, tool_count=tool_count, tool_calls_audit=tool_calls_audit,
					tool_results=tool_results, citations=citations, total_usage=total_usage,
					model=model, trace_id=trace_id, agent_span_id=agent_span_id,
				)
				if approval:
					yield {"type": "approval_required", "approval": approval}
					yield {
						"type": "paused", "status": "waiting_approval",
						"message": {"role": "assistant", "content": "该工具调用需要人工审批后才能继续。"},
						"model": model, "model_alias": self.settings.model, "trace_id": trace_id,
						"usage": total_usage.model_dump(), "approval": approval,
						"agent_steps": [step.model_dump() for step in steps],
						"tool_calls": tool_calls_audit, "tool_results": tool_results,
						"citations": self._dedupe_citations(citations),
					}
					return
				yield {
					"type": "tool_started", "call_id": call["id"],
					"tool": call["function"]["name"], "arguments": call["arguments"],
					"resumed": True,
				}
				tool_count, result, _latency = await self._execute_tool_call(
					request=request, call=call, model_step=pending_model_step,
					remaining_calls=pending_calls[index + 1:], messages=messages, steps=steps,
					tool_count=tool_count, tool_calls_audit=tool_calls_audit,
					tool_results=tool_results, citations=citations, total_usage=total_usage,
					model=model, trace_id=trace_id, agent_span_id=agent_span_id,
				)
				yield {
					"type": "tool_completed", "call_id": call["id"],
					"tool": result.get("tool"), "status": result.get("status"),
					"result_count": len(result.get("citations") or []), "resumed": True,
				}

		for step_no in range(start_model_step, self.settings.agent_max_steps + 1):
			payload["messages"] = messages
			yield {"type": "model_started", "step_no": step_no}
			decision_started_at = utc_now()
			message, usage, model, latency_ms = await self._model_call(
				payload=payload, request=request, trace_id=trace_id,
			)
			total_usage = self._merge_usage(total_usage, usage)
			self._enforce_token_budget(total_usage)
			steps.append(AgentStep(step_no=len(steps) + 1, type="model", status="completed", latency_ms=latency_ms))
			calls = self._parse_tool_calls(message, request.allowed_tools)
			decision_span_id = str(uuid.uuid4())
			await self._record_span(
				request=request, trace_id=trace_id, span_id=decision_span_id,
				parent_span_id=agent_span_id, name="agent.model_decision",
				started_at=decision_started_at, completed_at=utc_now(),
				input_data={"step_no": step_no, "available_tools": request.allowed_tools},
				output_data={
					"decision": "tool_calls" if calls else "final_answer",
					"tool_call_count": len(calls),
				}, metadata={"run_id": request.run_id, "step_no": step_no},
			)
			decision_checkpoint = None
			if calls:
				messages.append({
					"role": "assistant", "content": message.get("content"),
					"tool_calls": [
						{"id": call["id"], "type": "function", "function": call["function"]}
						for call in calls
					],
				})
				decision_checkpoint = self._build_checkpoint(
					request=request, stage="model_decision", next_model_step=step_no + 1,
					messages=messages, steps=steps, tool_count=tool_count,
					tool_calls=tool_calls_audit, pending_tool_calls=calls,
					tool_results=tool_results, citations=citations, usage=total_usage,
					model=model, trace_id=trace_id, agent_span_id=agent_span_id,
				)
			await self._persist_event(
				request=request, event_id=f"runtime:model_decision:{step_no}",
				step_type="model_decision", span_id=decision_span_id,
				data={
					"model_step": step_no,
					"decision": "tool_calls" if calls else "final_answer",
					"tools": [call["function"]["name"] for call in calls],
					"latency_ms": latency_ms,
				}, checkpoint=decision_checkpoint,
			)
			if not calls:
				content = str(message.get("content") or "").strip()
				if not content:
					raise RuntimeError("Agent model returned neither tool calls nor a final answer")
				try:
					output_guardrail = await self._checked_guardrail(
						request=request, trace_id=trace_id, parent_span_id=agent_span_id,
						name="agent.output_guardrail", checker=lambda: check_agent_output(content),
						input_data={"has_tool_results": bool(tool_results)},
					)
				except AgentRuntimeError as error:
					await self._persist_guardrail_failure(request=request, phase="output", error=error)
					raise
				steps.append(AgentStep(
					step_no=len(steps) + 1, type="guardrail", status="completed",
					guardrail_phase=output_guardrail.phase,
				))
				final_next_model_step = step_no + 1
				output_checkpoint = self._build_checkpoint(
					request=request, stage="output_guardrail", next_model_step=final_next_model_step,
					messages=messages, steps=steps, tool_count=tool_count,
					tool_calls=tool_calls_audit, pending_tool_calls=[],
					tool_results=tool_results, citations=citations, usage=total_usage,
					model=model, trace_id=trace_id, agent_span_id=agent_span_id,
					final_content=content,
				)
				await self._persist_event(
					request=request, event_id=self._event_id("output_guardrail", steps),
					step_type="output_guardrail", data={"status": output_guardrail.status},
					checkpoint=output_checkpoint,
				)
				output_checkpoint_saved = True
				yield {"type": "message_delta", "delta": content}
				break
			terminal_tool_result = False
			for index, call in enumerate(calls):
				approval = await self._request_approval_if_needed(
					request=request, call=call, model_step=step_no,
					remaining_calls=calls[index + 1:], messages=messages,
					steps=steps, tool_count=tool_count, tool_calls_audit=tool_calls_audit,
					tool_results=tool_results, citations=citations, total_usage=total_usage,
					model=model, trace_id=trace_id, agent_span_id=agent_span_id,
				)
				if approval:
					yield {"type": "approval_required", "approval": approval}
					yield {
						"type": "paused", "status": "waiting_approval",
						"message": {"role": "assistant", "content": "该工具调用需要人工审批后才能继续。"},
						"model": model, "model_alias": self.settings.model, "trace_id": trace_id,
						"usage": total_usage.model_dump(), "approval": approval,
						"agent_steps": [step.model_dump() for step in steps],
						"tool_calls": tool_calls_audit, "tool_results": tool_results,
						"citations": self._dedupe_citations(citations),
					}
					return
				tool_count += 1
				if tool_count > self.settings.agent_max_tool_calls:
					raise AgentRuntimeError(
						"AI_AGENT_TOOL_BUDGET_EXCEEDED", "Agent Run 已达到工具调用次数上限。",
					)
				yield {"type": "tool_started", "call_id": call["id"], "tool": call["function"]["name"], "arguments": call["arguments"]}
				tool_started_at = utc_now()
				started = time.perf_counter()
				result = await self._await_controlled(
					self.tool_client.execute(
						run_id=str(request.run_id or ""), call_id=call["id"], tool=call["function"]["name"],
						arguments=call["arguments"], capability_token=request.capability_token,
					),
					request=request,
				)
				result, tool_guardrail = sanitize_tool_result(result)
				latency = int((time.perf_counter() - started) * 1000)
				tool_guardrail_span_id = str(uuid.uuid4())
				await self._record_span(
					request=request, trace_id=trace_id, span_id=tool_guardrail_span_id,
					parent_span_id=agent_span_id, name="agent.tool_guardrail",
					started_at=tool_started_at, completed_at=utc_now(),
					input_data={"call_id": call["id"], "tool": call["function"]["name"]},
					output_data={
						"status": tool_guardrail.status,
						"sanitized_fields": tool_guardrail.sanitized_fields,
					}, metadata={"run_id": request.run_id, "call_id": call["id"]},
				)
				await self._persist_event(
					request=request, event_id=f"runtime:tool_guardrail:{call['id']}"[:140],
					step_type="tool_guardrail", span_id=tool_guardrail_span_id,
					data={
						"call_id": call["id"], "tool": result.get("tool"),
						"status": tool_guardrail.status,
						"sanitized_fields": tool_guardrail.sanitized_fields,
					},
				)
				steps.append(AgentStep(
					step_no=len(steps) + 1, type="guardrail", status="completed",
					call_id=call["id"], tool=result.get("tool"), guardrail_phase=tool_guardrail.phase,
				))
				await self._record_span(
					request=request, trace_id=trace_id, span_id=str(uuid.uuid4()),
					parent_span_id=agent_span_id, name=f"agent-tool:{result.get('tool')}",
					started_at=tool_started_at, completed_at=utc_now(),
					input_data={"call_id": call["id"], "tool": call["function"]["name"], "arguments": call["arguments"]},
					output_data={"status": result.get("status"), "result_count": len(result.get("citations") or [])},
					metadata={"call_id": call["id"], "tool": result.get("tool")},
					error=(result.get("error") or {}).get("code"),
				)
				steps.append(AgentStep(
					step_no=len(steps) + 1, type="tool", status="completed", call_id=call["id"],
					tool=result.get("tool"), result_status=result.get("status"), latency_ms=latency,
					error_code=(result.get("error") or {}).get("code"),
				))
				tool_calls_audit.append({"call_id": call["id"], "tool": result.get("tool"), "arguments": call["arguments"], "status": result.get("status"), "result_count": len(result.get("citations") or [])})
				tool_results.append(result)
				citations.extend(result.get("citations") or [])
				messages.append(self._tool_message(result))
				tool_checkpoint = self._build_checkpoint(
					request=request, stage="tool_completed", next_model_step=step_no + 1,
					messages=messages, steps=steps, tool_count=tool_count,
					tool_calls=tool_calls_audit, pending_tool_calls=calls[index + 1:],
					tool_results=tool_results, citations=citations, usage=total_usage,
					model=model, trace_id=trace_id, agent_span_id=agent_span_id,
				)
				await self._persist_event(
					request=request,
					event_id=self._event_id("checkpoint", steps, call_id=call["id"]),
					step_type="checkpoint", data={
						"stage": "tool_completed", "model_step": step_no,
						"call_id": call["id"], "tool": result.get("tool"),
						"remaining_tool_calls": len(calls[index + 1:]),
					}, checkpoint=tool_checkpoint,
				)
				yield {"type": "tool_completed", "call_id": call["id"], "tool": result.get("tool"), "status": result.get("status"), "result_count": len(result.get("citations") or [])}
				terminal_tool_result = result.get("status") not in {"not_found", "retryable_error"}

			if terminal_tool_result or tool_count >= self.settings.agent_max_tool_calls:
				payload["messages"] = messages
				grounded_stream = self._stream_grounded_answer(
					payload=payload, request=request, trace_id=trace_id,
					parent_span_id=agent_span_id,
				)
				async for streamed in self._controlled_events(grounded_stream, request=request):
					if streamed["type"] == "message_delta":
						yield streamed
						continue
					content = streamed["content"]
					model = streamed["model"]
					first_token_ms = streamed["first_token_ms"]
					total_usage = self._merge_usage(total_usage, streamed["usage"])
					self._enforce_token_budget(total_usage)
					steps.append(AgentStep(
						step_no=len(steps) + 1, type="model", status="completed",
						latency_ms=streamed["latency_ms"],
					))
				final_next_model_step = step_no + 1
				break
		else:
			raise AgentRuntimeError(
				"AI_AGENT_STEP_BUDGET_EXCEEDED", "Agent Run 已达到模型步骤上限，仍未形成最终回答。",
			)

		if not output_checkpoint_saved:
			if output_guardrail is None:
				try:
					output_guardrail = await self._checked_guardrail(
						request=request, trace_id=trace_id, parent_span_id=agent_span_id,
						name="agent.output_guardrail", checker=lambda: check_agent_output(content),
						input_data={"has_tool_results": bool(tool_results)},
					)
				except AgentRuntimeError as error:
					await self._persist_guardrail_failure(request=request, phase="output", error=error)
					raise
			steps.append(AgentStep(
				step_no=len(steps) + 1, type="guardrail", status="completed",
				guardrail_phase=output_guardrail.phase,
			))
			output_checkpoint = self._build_checkpoint(
				request=request, stage="output_guardrail", next_model_step=final_next_model_step,
				messages=messages, steps=steps, tool_count=tool_count,
				tool_calls=tool_calls_audit, pending_tool_calls=[], tool_results=tool_results,
				citations=citations, usage=total_usage, model=model, trace_id=trace_id,
				agent_span_id=agent_span_id, final_content=content,
			)
			await self._persist_event(
				request=request, event_id=self._event_id("output_guardrail", steps),
				step_type="output_guardrail", data={"status": output_guardrail.status},
				checkpoint=output_checkpoint,
			)

		for warning in self.model_client._warnings(request):
			yield {"type": "warning", "message": warning}
		await self._record_span(
			request=request, trace_id=trace_id, span_id=agent_span_id,
			name="agent-run", started_at=agent_started_at, completed_at=utc_now(),
			input_data={"allowed_tools": request.allowed_tools},
			output_data={"steps": len(steps), "tool_calls": tool_count, "status": "completed"},
			metadata={"run_id": request.run_id},
		)
		yield {
			"type": "completed", "message": {"role": "assistant", "content": content},
			"model": model, "model_alias": self.settings.model, "trace_id": trace_id,
			"usage": total_usage.model_dump(), "warnings": self.model_client._warnings(request),
			"first_token_ms": first_token_ms,
			"agent_steps": [step.model_dump() for step in steps],
			"tool_calls": tool_calls_audit, "tool_results": tool_results,
			"citations": self._dedupe_citations(citations),
		}
