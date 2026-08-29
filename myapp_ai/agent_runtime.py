from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from contextlib import suppress

from .agent_guardrails import (
	AgentRuntimeError,
	check_agent_grounding,
	check_agent_input,
	check_agent_output,
	sanitize_tool_result,
)
from .agent_tool_client import AgentToolClient
from .agent_tools import tool_approval_policy, tool_definitions, validate_tool_arguments
from .langfuse_client import utc_now
from .litellm_client import LiteLLMClient
from .schemas import AgentCheckpoint, AgentRequest, AgentResponse, AgentStep, ChatMessage, TokenUsage

_OUTPUT_STREAM_GUARDRAIL_HOLDBACK_CHARS = 256
logger = logging.getLogger(__name__)

_AGENT_ENTITY_SLOT_TYPES = {
	"product": {"product"},
	"business_document": {
		"sales_order", "sales_invoice", "purchase_order", "purchase_invoice",
	},
	"business_partner": {"customer", "supplier"},
}
_AGENT_ENTITY_STATUSES = {"resolved", "ambiguous", "not_found"}
_PRODUCT_QUOTED_LITERAL = re.compile(
	r"(?:带有?|含有?|包含)\s*[‘“\"']\s*([^’”\"']{1,20}?)\s*[’”\"']"
)
_PRODUCT_CHARACTER_LITERAL = re.compile(
	r"(?:带有?|含有?|包含)\s*([\u3400-\u9fffA-Za-z0-9])\s*(?:字|字符)"
)


def _bounded_context_text(value, *, limit: int) -> str | None:
	text = str(value or "").strip()
	return text[:limit] or None


def _agent_conversation_state(context) -> dict | None:
	"""Keep only server-owned entity references needed to choose tool arguments."""
	if not isinstance(context, dict):
		return None
	state = context.get("conversation_state")
	if not isinstance(state, dict):
		return None
	result = {
		"schema_version": "conversation-state-v2",
		"active_scenario": _bounded_context_text(state.get("active_scenario"), limit=40) or "general",
	}
	active_entities = state.get("active_entities")
	if not isinstance(active_entities, dict):
		return result
	normalized_entities = {}
	for slot, allowed_types in _AGENT_ENTITY_SLOT_TYPES.items():
		entity = active_entities.get(slot)
		if not isinstance(entity, dict):
			continue
		entity_type = _bounded_context_text(entity.get("entity_type"), limit=40)
		status = _bounded_context_text(entity.get("resolution_status"), limit=20)
		entity_id = _bounded_context_text(entity.get("entity_id"), limit=140)
		if entity_type not in allowed_types or status not in _AGENT_ENTITY_STATUSES:
			continue
		if status == "resolved" and not entity_id:
			continue
		normalized_entities[slot] = {
			"entity_type": entity_type,
			"entity_id": entity_id if status == "resolved" else None,
			"display_name": _bounded_context_text(entity.get("display_name"), limit=140),
			"resolution_status": status,
		}
	if normalized_entities:
		result["active_entities"] = normalized_entities
	return result


class AgentEngine:
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
	def _check_output(content: str, *, tool_results: list[dict], company: str):
		check_agent_output(content)
		return check_agent_grounding(content, tool_results=tool_results, company=company)

	@staticmethod
	def _tool_answer_reminder(tool_results: list[dict]) -> dict:
		tools = {str(result.get("tool") or "") for result in tool_results}
		instructions = [
			"受控工具已经完成。现在必须直接回答用户当前查询并使用现有 tool 结果；"
			"禁止退回通用欢迎语、再次询问用户要查什么或忽略工具结果。",
		]
		if "search_products" in tools:
			instructions.append(
				"商品结果数量只能明确写成返回结果数或匹配商品数；"
				"工具没有库存字段时不得写成库存、现有数量或可用数量。"
			)
		if "query_business_documents" in tools:
			instructions.append(
				"单据摘要必须明确写出中文单据类型、非 all 的状态筛选和返回数量；"
				"提到单据号时，同一句写出中文单据类型和工具返回状态。"
			)
		return {"role": "system", "content": "".join(instructions)}

	@staticmethod
	def _grounding_rewrite_instruction(details: list[str]) -> str:
		instructions = [
			"上一个候选回答未通过业务事实校验。请只依据现有 tool 消息重写一次；"
			"不得新增标识符、数字、状态、公司或完整性结论。",
		]
		if "tool_answer" in details:
			instructions.append(
				"上一个回答没有实际使用已经返回的工具结果。必须直接回答本轮查询，"
				"至少复述一个受控结果事实，禁止通用欢迎语或重新索要查询事项。"
			)
		if any(detail.startswith("quantity:") for detail in details):
			instructions.append(
				"工具没有支持被拦截的库存或业务数量；不要使用库存、数量、现有或可用等措辞。"
				"如果数字只是结果条数，请明确写成返回结果数或匹配商品数。"
			)
		return "".join(instructions)

	@staticmethod
	def _deterministic_tool_answer(tool_results: list[dict]) -> str | None:
		entity_labels = {
			"sales_order": "销售订单", "sales_invoice": "销售发票",
			"purchase_order": "采购订单", "purchase_invoice": "采购发票",
			"Sales Order": "销售订单", "Sales Invoice": "销售发票",
			"Purchase Order": "采购订单", "Purchase Invoice": "采购发票",
		}
		status_labels = {
			"unfinished": "未完成", "completed": "已完成", "cancelled": "已取消",
			"draft": "草稿", "paid": "已付款", "unpaid": "未付款",
		}
		report_labels = {
			"overview": "经营总览", "sales": "销售报表", "purchase": "采购报表",
			"cashflow": "现金流报表", "receivable_payable": "应收应付报表",
		}
		# A bounded product-search retry can first return no matches and then
		# succeed after removing an unsupported model hint.  The earlier empty
		# result is superseded by that later candidate set and must not produce a
		# contradictory "not found" sentence in the final answer.  Empty results
		# after the last successful product search remain visible because they may
		# represent a separate user-requested lookup.
		last_nonempty_product_result = -1
		for index, result in enumerate(tool_results):
			if str(result.get("tool") or "") != "search_products":
				continue
			context = result.get("model_context") or {}
			products = context.get("products") or []
			result_count = int((result.get("data") or {}).get("result_count") or 0)
			if products or result_count > 0:
				last_nonempty_product_result = index

		summaries = []
		for index, result in enumerate(tool_results):
			status = str(result.get("status") or "")
			context = result.get("model_context") or {}
			tool = str(result.get("tool") or "")
			if tool == "search_products":
				products = context.get("products") or []
				if status == "not_found":
					if index < last_nonempty_product_result:
						continue
					summaries.append("未找到匹配商品。")
					continue
				if not products:
					return None
				items = []
				for product in products[:8]:
					name = str(product.get("item_name") or product.get("label") or "商品").strip()
					code = str(product.get("item_code") or product.get("id") or "").strip()
					items.append(f"{name}（{code}）" if code else name)
				count = int((result.get("data") or {}).get("result_count") or len(products))
				if status == "ambiguous":
					summaries.append(
						f"查询到 {count} 个待确认商品候选：{'、'.join(items)}。"
						"请结合品牌、规格、口味或包装确认所需商品。"
					)
				else:
					summaries.append(f"查询到 {count} 个匹配商品：{'、'.join(items)}。")
				continue
			if tool == "get_business_report":
				if status != "resolved":
					return None
				report = context.get("report") or {}
				report_type = str(
					report.get("report_type") or (result.get("data") or {}).get("report_type") or ""
				)
				label = report_labels.get(report_type)
				if not label:
					return None
				summaries.append(f"{label}查询已完成，具体指标由界面展示。")
				continue
			if tool != "query_business_documents" or status not in {"resolved", "not_found"}:
				return None
			result_set = context.get("result_set") or {}
			scope = result_set.get("scope") or context.get("dsl") or {}
			groups = result_set.get("groups") or context.get("document_groups") or []
			documents = context.get("documents") or []
			entities = [
				entity_labels.get(str(group.get("entity") or ""), str(group.get("entity") or "业务单据"))
				for group in groups
			]
			if not entities:
				entities = list(dict.fromkeys(
					entity_labels.get(str(document.get("doctype") or ""), "业务单据")
					for document in documents
				))
			returned_count = sum(int(group.get("returned_count") or 0) for group in groups)
			if not groups:
				returned_count = len(documents)
			parts = []
			if scope.get("company"):
				parts.append(str(scope["company"]))
			if scope.get("date_from") and scope.get("date_to"):
				parts.append(f"{scope['date_from']} 至 {scope['date_to']}")
			status_filter = str(scope.get("status_filter") or "")
			if status_filter and status_filter != "all":
				parts.append(status_labels.get(status_filter, status_filter))
			parts.append("、".join(dict.fromkeys(entities)) or "业务单据")
			prefix = "，".join(parts)
			if status == "not_found" or returned_count == 0:
				summaries.append(f"已查询{prefix}，未找到匹配单据。")
				continue
			lines = [f"已查询{prefix}，共返回 {returned_count} 张。"]
			for document in documents[:5]:
				doctype = entity_labels.get(str(document.get("doctype") or ""), "业务单据")
				name = str(document.get("name") or "").strip()
				document_status = str(document.get("status") or "").strip()
				if name and document_status:
					lines.append(f"{doctype} {name}，状态为{document_status}。")
				elif name:
					lines.append(f"{doctype} {name}。")
			summaries.append("".join(lines))
		return "\n".join(summaries) if summaries else None

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
			logger.error(
				"Agent runtime event persistence failed run_id=%s event_id=%s step_type=%s error=%s",
				request.run_id, event_id, step_type, error,
			)
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
		# Business facts must still arrive through tool messages. Only the
		# server-owned typed entity state may enter the model prompt so a phrase
		# such as "这个订单" can become an exact, revalidated tool query.
		conversation_state = _agent_conversation_state(request.context)
		request = request.model_copy(update={
			"context": (
				{"conversation_state": conversation_state}
				if conversation_state
				else None
			),
		})
		payload, trace_id, normalized = self.model_client._build_payload(request)
		payload["tools"] = tool_definitions(request.allowed_tools)
		payload["tool_choice"] = "auto"
		return payload, trace_id, normalized

	@staticmethod
	def _explicit_product_literal(request: AgentRequest) -> str | None:
		content = next(
			(message.content for message in reversed(request.messages) if message.role == "user"),
			"",
		)
		for pattern in (_PRODUCT_QUOTED_LITERAL, _PRODUCT_CHARACTER_LITERAL):
			match = pattern.search(content)
			if match and (literal := match.group(1).strip()):
				return literal
		return None

	@classmethod
	def _normalize_explicit_tool_filters(
		cls, calls: list[dict], request: AgentRequest,
	) -> list[dict]:
		literal = cls._explicit_product_literal(request)
		if not literal:
			return calls
		for call in calls:
			if call["function"]["name"] != "search_products":
				continue
			arguments = dict(call["arguments"])
			arguments["query"] = literal
			arguments["query_variants"] = []
			arguments["hypotheses"] = []
			arguments["attributes"] = {
				"brand": None, "item_group": None, "color": None, "flavor": None,
				"specification": None, "capacity": None, "packaging": None,
			}
			arguments["match_mode"] = "contains"
			call["arguments"] = arguments
			call["function"]["arguments"] = json.dumps(
				arguments, ensure_ascii=False, separators=(",", ":"),
			)
		return calls

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

	async def _stream_model_turn(
		self, *, payload: dict, request: AgentRequest, trace_id: str, parent_span_id: str,
		allow_tools: bool, emit_output: bool = True,
	):
		if not self.model_client.async_client:
			raise RuntimeError("Shared LiteLLM AsyncClient is not configured")
		generation_id = str(uuid.uuid4())
		started_at = utc_now()
		started = time.perf_counter()
		first_token_ms = None
		content_parts: list[str] = []
		pending_output = ""
		tool_call_parts: dict[int, dict] = {}
		usage = TokenUsage()
		model = self.settings.model
		stream_payload = self.model_client._fit_payload_context({
			**payload,
			"stream": True,
			"stream_options": {"include_usage": True},
			"tool_choice": "auto" if allow_tools else "none",
		})
		try:
			for attempt in range(1, self.settings.provider_max_attempts + 1):
				try:
					stream_context = self.model_client.async_client.stream(
						"POST",
						"/v1/chat/completions",
						headers={
							"Authorization": f"Bearer {self.settings.litellm_api_key}",
							"Content-Type": "application/json",
							"X-MyApp-Trace-Id": trace_id,
						},
						json=stream_payload,
					)
					response = await stream_context.__aenter__()
					response.raise_for_status()
					break
				except Exception as error:
					with suppress(Exception):
						await stream_context.__aexit__(None, None, None)
					if (
						attempt >= self.settings.provider_max_attempts
						or not self.model_client._is_transient_provider_error(error)
					):
						raise
					await self.model_client._provider_retry_wait(attempt)
			try:
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
					delta_payload = choice.get("delta") or {}
					for fallback_index, raw_call in enumerate(delta_payload.get("tool_calls") or []):
						if content_parts:
							raise AgentRuntimeError(
								"AI_AGENT_MODEL_RESPONSE_INVALID",
								"模型在同一步中混合返回了文本和工具调用。",
							)
						index = int(raw_call.get("index", fallback_index))
						current = tool_call_parts.setdefault(index, {
							"id": "", "type": "function", "function": {"name": "", "arguments": ""},
						})
						if raw_call.get("id"):
							current["id"] = str(raw_call["id"])
						if raw_call.get("type"):
							current["type"] = str(raw_call["type"])
						function = raw_call.get("function") or {}
						if function.get("name"):
							current["function"]["name"] += str(function["name"])
						if function.get("arguments"):
							current["function"]["arguments"] += str(function["arguments"])
					delta = str(delta_payload.get("content") or "")
					if delta:
						if tool_call_parts:
							raise AgentRuntimeError(
								"AI_AGENT_MODEL_RESPONSE_INVALID",
								"模型在同一步中混合返回了工具调用和文本。",
							)
						content_parts.append(delta)
						# Keep a bounded suffix until the complete output guardrail has
						# enough look-ahead to detect secrets split across provider deltas.
						# A delta must never be emitted before this scan succeeds.
						check_agent_output("".join(content_parts))
						pending_output += delta
						if emit_output and len(pending_output) > _OUTPUT_STREAM_GUARDRAIL_HOLDBACK_CHARS:
							safe_delta = pending_output[:-_OUTPUT_STREAM_GUARDRAIL_HOLDBACK_CHARS]
							pending_output = pending_output[-_OUTPUT_STREAM_GUARDRAIL_HOLDBACK_CHARS:]
							if safe_delta:
								if first_token_ms is None:
									first_token_ms = int((time.perf_counter() - started) * 1000)
								yield {"type": "output_delta", "delta": safe_delta}
				if content_parts:
					check_agent_output("".join(content_parts))
				if emit_output and pending_output and not tool_call_parts:
					if first_token_ms is None:
						first_token_ms = int((time.perf_counter() - started) * 1000)
					yield {"type": "output_delta", "delta": pending_output}
			finally:
				await stream_context.__aexit__(None, None, None)
		except Exception as error:
			if isinstance(error, AgentRuntimeError) and error.code == "AI_AGENT_OUTPUT_BLOCKED":
				with suppress(Exception):
					await self._persist_guardrail_failure(request=request, phase="output", error=error)
				await self._record_span(
					request=request, trace_id=trace_id, span_id=str(uuid.uuid4()),
					parent_span_id=parent_span_id, name="agent.output_guardrail",
					started_at=started_at, completed_at=utc_now(),
					input_data={"streaming": True, "allow_tools": allow_tools},
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
		tool_calls = [tool_call_parts[index] for index in sorted(tool_call_parts)]
		if not content and not tool_calls:
			await self.model_client._arecord_generation(
				request=request, trace_id=trace_id, generation_id=generation_id,
				started_at=started_at, completed_at=utc_now(), model=model,
				model_alias=self.settings.model, output="", usage=usage,
				error="EmptyModelResponse",
			)
			raise RuntimeError("Agent model returned neither tool calls nor content")
		message = {
			"role": "assistant",
			"content": content or None,
		}
		if tool_calls:
			message["tool_calls"] = tool_calls
		await self.model_client._arecord_generation(
			request=request, trace_id=trace_id, generation_id=generation_id,
			started_at=started_at, completed_at=utc_now(), model=model,
			model_alias=self.settings.model,
			output=content or json.dumps({"tool_calls": tool_calls}, ensure_ascii=False), usage=usage,
		)
		yield {
			"type": "model_decision_completed",
			"message": message,
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

	async def run(self, request: AgentRequest) -> AgentResponse:
		return await self._collect_terminal(self.events(request))

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
		return await self._collect_terminal(self.resume_events(request))

	async def _collect_terminal(self, events) -> AgentResponse:
		async for event in events:
			if event["type"] not in {"run_completed", "run_paused"}:
				continue
			return AgentResponse(
				status="waiting_approval" if event["type"] == "run_paused" else "completed",
				message=ChatMessage.model_validate(event["message"]),
				model=event["model"], model_alias=event["model_alias"],
				trace_id=event["trace_id"], usage=TokenUsage.model_validate(event["usage"]),
				warnings=list(event.get("warnings") or []),
				agent_steps=[AgentStep.model_validate(step) for step in event.get("agent_steps") or []],
				tool_calls=list(event.get("tool_calls") or []),
				tool_results=list(event.get("tool_results") or []),
				citations=list(event.get("citations") or []),
				approval=event.get("approval"),
			)
		raise RuntimeError("Agent Engine ended without a terminal event")

	async def events(self, request: AgentRequest):
		try:
			async with asyncio.timeout(self.settings.agent_run_timeout_seconds):
				async for event in self._stream(request):
					yield event
		except TimeoutError as error:
			raise AgentRuntimeError(
				"AI_AGENT_DEADLINE_EXCEEDED", "Agent Run 已超过统一执行时限。", retryable=True,
			) from error

	async def resume_events(self, request: AgentRequest):
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
		# The engine owns one resumable loop. After the first tool result, model
		# turns use upstream SSE so they may either request another tool or produce
		# the final answer without changing semantics between transport consumers.
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
			"type": "run_started", "trace_id": trace_id, "model_alias": self.settings.model,
			"resumed": bool(checkpoint),
		}
		if checkpoint and checkpoint.stage == "output_guardrail" and checkpoint.final_content:
			content = checkpoint.final_content
			yield {"type": "output_delta", "delta": content, "replayed": True}
			for warning in self.model_client._warnings(request):
				yield {"type": "run_warning", "message": warning}
			yield {
				"type": "run_completed", "message": {"role": "assistant", "content": content},
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
					yield {"type": "tool_approval_required", "approval": approval}
					yield {
						"type": "run_paused", "status": "waiting_approval",
						"message": {"role": "assistant", "content": "该工具调用需要人工审批后才能继续。"},
						"model": model, "model_alias": self.settings.model, "trace_id": trace_id,
						"usage": total_usage.model_dump(), "approval": approval,
						"warnings": self.model_client._warnings(request),
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
			payload["messages"] = (
				[*messages, self._tool_answer_reminder(tool_results)]
				if tool_results else messages
			)
			yield {"type": "model_started", "step_no": step_no}
			decision_started_at = utc_now()
			content_streamed = False
			if tool_results:
				streamed_turn = self._stream_model_turn(
					payload=payload, request=request, trace_id=trace_id,
					parent_span_id=agent_span_id,
					allow_tools=tool_count < self.settings.agent_max_tool_calls,
					emit_output=False,
				)
				async for streamed in self._controlled_events(streamed_turn, request=request):
					if streamed["type"] == "output_delta":
						content_streamed = True
						yield streamed
						continue
					message = streamed["message"]
					usage = streamed["usage"]
					model = streamed["model"]
					latency_ms = streamed["latency_ms"]
					if streamed["first_token_ms"] is not None:
						first_token_ms = streamed["first_token_ms"]
			else:
				message, usage, model, latency_ms = await self._model_call(
					payload=payload, request=request, trace_id=trace_id,
				)
			total_usage = self._merge_usage(total_usage, usage)
			self._enforce_token_budget(total_usage)
			steps.append(AgentStep(step_no=len(steps) + 1, type="model", status="completed", latency_ms=latency_ms))
			calls = self._normalize_explicit_tool_filters(
				self._parse_tool_calls(message, request.allowed_tools), request,
			)
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
					self._check_output(
						content, tool_results=tool_results, company=request.company,
					)
				except AgentRuntimeError as error:
					if error.code != "AI_AGENT_OUTPUT_GROUNDING_FAILED":
						raise
					rewrite_started_at = utc_now()
					rewrite_payload = {
						**payload,
						"messages": [
							*messages,
							{"role": "assistant", "content": content},
							{
								"role": "system",
								"content": self._grounding_rewrite_instruction(error.details),
							},
						],
					}
					rewrite_decision = None
					rewrite_turn = self._stream_model_turn(
						payload=rewrite_payload, request=request, trace_id=trace_id,
						parent_span_id=agent_span_id, allow_tools=False, emit_output=False,
					)
					async for rewritten in self._controlled_events(rewrite_turn, request=request):
						if rewritten["type"] == "model_decision_completed":
							rewrite_decision = rewritten
					if rewrite_decision is None or rewrite_decision["message"].get("tool_calls"):
						raise AgentRuntimeError(
							"AI_AGENT_OUTPUT_GROUNDING_FAILED",
							"模型未能生成可验证的业务回答。",
						)
					content = str(rewrite_decision["message"].get("content") or "").strip()
					total_usage = self._merge_usage(total_usage, rewrite_decision["usage"])
					self._enforce_token_budget(total_usage)
					model = rewrite_decision["model"]
					steps.append(AgentStep(
						step_no=len(steps) + 1, type="model", status="completed",
						latency_ms=rewrite_decision["latency_ms"],
					))
					await self._record_span(
						request=request, trace_id=trace_id, span_id=str(uuid.uuid4()),
						parent_span_id=agent_span_id, name="agent.grounding_rewrite",
						started_at=rewrite_started_at, completed_at=utc_now(),
						input_data={"violations": error.details},
						output_data={"status": "completed"},
						metadata={"run_id": request.run_id, "model_step": step_no},
					)
					await self._persist_event(
						request=request, event_id=f"runtime:grounding_rewrite:{step_no}",
						step_type="grounding_rewrite",
						data={"status": "completed", "violations": error.details},
					)
					if first_token_ms is None:
						first_token_ms = rewrite_decision["latency_ms"]
				try:
					output_guardrail = await self._checked_guardrail(
						request=request, trace_id=trace_id, parent_span_id=agent_span_id,
						name="agent.output_guardrail",
						checker=lambda: self._check_output(
							content, tool_results=tool_results, company=request.company,
						),
						input_data={"has_tool_results": bool(tool_results)},
					)
				except AgentRuntimeError as error:
					fallback = (
						self._deterministic_tool_answer(tool_results)
						if set(error.details) == {"tool_answer"} else None
					)
					if not fallback:
						await self._persist_guardrail_failure(request=request, phase="output", error=error)
						raise
					content = fallback
					fallback_started_at = utc_now()
					try:
						output_guardrail = await self._checked_guardrail(
							request=request, trace_id=trace_id, parent_span_id=agent_span_id,
							name="agent.output_guardrail",
							checker=lambda: self._check_output(
								content, tool_results=tool_results, company=request.company,
							),
							input_data={"has_tool_results": True, "deterministic_fallback": True},
						)
					except AgentRuntimeError as fallback_error:
						await self._persist_guardrail_failure(
							request=request, phase="output", error=fallback_error,
						)
						raise
					await self._record_span(
						request=request, trace_id=trace_id, span_id=str(uuid.uuid4()),
						parent_span_id=agent_span_id, name="agent.deterministic_tool_answer",
						started_at=fallback_started_at, completed_at=utc_now(),
						input_data={"tools": [result.get("tool") for result in tool_results]},
						output_data={"status": "completed"},
						metadata={"run_id": request.run_id, "model_step": step_no},
					)
					await self._persist_event(
						request=request, event_id=f"runtime:deterministic_tool_answer:{step_no}",
						step_type="deterministic_tool_answer",
						data={"status": "completed", "tools": [
							result.get("tool") for result in tool_results
						]},
					)
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
				if not content_streamed:
					if first_token_ms is None:
						first_token_ms = latency_ms
					yield {"type": "output_delta", "delta": content}
				break
			for index, call in enumerate(calls):
				approval = await self._request_approval_if_needed(
					request=request, call=call, model_step=step_no,
					remaining_calls=calls[index + 1:], messages=messages,
					steps=steps, tool_count=tool_count, tool_calls_audit=tool_calls_audit,
					tool_results=tool_results, citations=citations, total_usage=total_usage,
					model=model, trace_id=trace_id, agent_span_id=agent_span_id,
				)
				if approval:
					yield {"type": "tool_approval_required", "approval": approval}
					yield {
						"type": "run_paused", "status": "waiting_approval",
						"message": {"role": "assistant", "content": "该工具调用需要人工审批后才能继续。"},
						"model": model, "model_alias": self.settings.model, "trace_id": trace_id,
						"usage": total_usage.model_dump(), "approval": approval,
						"warnings": self.model_client._warnings(request),
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
		else:
			raise AgentRuntimeError(
				"AI_AGENT_STEP_BUDGET_EXCEEDED", "Agent Run 已达到模型步骤上限，仍未形成最终回答。",
			)

		if not output_checkpoint_saved:
			if output_guardrail is None:
				try:
					output_guardrail = await self._checked_guardrail(
						request=request, trace_id=trace_id, parent_span_id=agent_span_id,
						name="agent.output_guardrail",
						checker=lambda: self._check_output(
							content, tool_results=tool_results, company=request.company,
						),
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
			yield {"type": "run_warning", "message": warning}
		await self._record_span(
			request=request, trace_id=trace_id, span_id=agent_span_id,
			name="agent-run", started_at=agent_started_at, completed_at=utc_now(),
			input_data={"allowed_tools": request.allowed_tools},
			output_data={"steps": len(steps), "tool_calls": tool_count, "status": "completed"},
			metadata={"run_id": request.run_id},
		)
		yield {
			"type": "run_completed", "message": {"role": "assistant", "content": content},
			"model": model, "model_alias": self.settings.model, "trace_id": trace_id,
			"usage": total_usage.model_dump(), "warnings": self.model_client._warnings(request),
			"first_token_ms": first_token_ms,
			"agent_steps": [step.model_dump() for step in steps],
			"tool_calls": tool_calls_audit, "tool_results": tool_results,
			"citations": self._dedupe_citations(citations),
		}


class AgentRuntime:
	"""Transport adapter over the single event-driven AgentEngine."""

	_EVENT_TYPES = {
		"run_started": "started",
		"output_delta": "message_delta",
		"tool_approval_required": "approval_required",
		"run_paused": "paused",
		"run_warning": "warning",
		"run_completed": "completed",
	}

	def __init__(self, model_client: LiteLLMClient, tool_client: AgentToolClient):
		self.engine = AgentEngine(model_client, tool_client)

	async def run(self, request: AgentRequest) -> AgentResponse:
		return await self.engine.run(request)

	async def resume(self, request: AgentRequest) -> AgentResponse:
		return await self.engine.resume(request)

	@classmethod
	def _transport_event(cls, event: dict) -> dict:
		transport = dict(event)
		transport["type"] = cls._EVENT_TYPES.get(event["type"], event["type"])
		return transport

	async def stream(self, request: AgentRequest):
		async for event in self.engine.events(request):
			yield self._transport_event(event)

	async def resume_stream(self, request: AgentRequest):
		async for event in self.engine.resume_events(request):
			yield self._transport_event(event)
