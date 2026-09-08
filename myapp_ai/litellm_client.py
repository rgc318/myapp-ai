from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import uuid

import httpx

from .config import Settings
from .langfuse_client import LangfuseClient, utc_now
from .prompts import get_prompt_spec, with_effective_prompt
from .schemas import (
	AgentRequest,
	ChatMessage,
	ChatRequest,
	ChatResponse,
	IntentParseCandidate,
	IntentParseResponse,
	InventoryAdjustmentDraftCandidate,
	InventoryAdjustmentDraftResponse,
	ProductSetupDraftCandidate,
	ProductSetupDraftResponse,
	PurchaseOrderDraftCandidate,
	PurchaseOrderDraftResponse,
	SalesOrderDraftCandidate,
	SalesOrderDraftResponse,
	TokenUsage,
)


def _strict_response_schema(schema_class) -> dict:
	"""Build an OpenAI strict JSON Schema without changing local model defaults."""
	schema = schema_class.model_json_schema()

	def normalize(node) -> None:
		if isinstance(node, dict):
			properties = node.get("properties")
			if isinstance(properties, dict):
				node["additionalProperties"] = False
				node["required"] = list(properties)
			for value in node.values():
				normalize(value)
		elif isinstance(node, list):
			for value in node:
				normalize(value)

	normalize(schema)
	return schema


class LiteLLMClient:
	def __init__(
		self,
		settings: Settings,
		transport: httpx.BaseTransport | None = None,
		langfuse_client: LangfuseClient | None = None,
		async_client: httpx.AsyncClient | None = None,
	):
		self.settings = settings
		self.transport = transport
		self.async_client = async_client
		self.langfuse = langfuse_client or LangfuseClient(settings)

	@staticmethod
	def _estimate_tokens(value) -> int:
		if isinstance(value, dict):
			value = {
				key: (
					{"url": "<image>"}
					if key == "image_url" and isinstance(item, dict)
					else LiteLLMClient._token_estimate_value(item)
				)
				for key, item in value.items()
			}
		elif isinstance(value, list):
			value = [LiteLLMClient._token_estimate_value(item) for item in value]
		text = value if isinstance(value, str) else json.dumps(
			value, ensure_ascii=False, separators=(",", ":"),
		)
		cjk = len(re.findall(r"[\u3400-\u9fff\uf900-\ufaff]", text))
		non_cjk = len(text) - cjk
		return max(1, cjk + math.ceil(non_cjk / 4))

	@staticmethod
	def _token_estimate_value(value):
		if isinstance(value, dict):
			if value.get("type") == "image_url":
				return {"type": "image_url", "image_url": {"url": "<image>"}}
			return {key: LiteLLMClient._token_estimate_value(item) for key, item in value.items()}
		if isinstance(value, list):
			return [LiteLLMClient._token_estimate_value(item) for item in value]
		return value

	@staticmethod
	def _provider_messages(request: ChatRequest) -> list[dict]:
		message_attachment_ids = {
			attachment.attachment_id
			for message in request.messages
			for attachment in message.attachments
		}
		attachments_by_index = {
			index: list(message.attachments)
			for index, message in enumerate(request.messages)
			if message.attachments
		}
		legacy_attachments = [
			attachment for attachment in request.attachments
			if attachment.attachment_id not in message_attachment_ids
		]
		if legacy_attachments:
			last_user_index = next(
				(index for index in range(len(request.messages) - 1, -1, -1)
				 if request.messages[index].role == "user"),
				None,
			)
			if last_user_index is None:
				raise RuntimeError("Image attachments require a user message")
			attachments_by_index.setdefault(last_user_index, []).extend(legacy_attachments)

		messages = []
		for index, message in enumerate(request.messages):
			row = message.model_dump(exclude={"attachments"})
			attachments = attachments_by_index.get(index) or []
			if attachments:
				row["content"] = [
					{"type": "text", "text": message.content},
				]
				for attachment in attachments:
					row["content"].extend([
						{
							"type": "text",
							"text": (
								"<image_attachment attachment_id=\""
								f"{attachment.attachment_id}\" />"
							),
						},
						{
							"type": "image_url",
							"image_url": {
								"url": f"data:{attachment.mime_type};base64,{attachment.data_base64}",
							},
						},
					])
			messages.append(row)
		return messages

	@classmethod
	def _message_units(cls, messages: list[dict]) -> list[list[dict]]:
		units: list[list[dict]] = []
		index = 0
		while index < len(messages):
			message = messages[index]
			unit = [message]
			index += 1
			if message.get("role") == "assistant" and message.get("tool_calls"):
				while index < len(messages) and messages[index].get("role") == "tool":
					unit.append(messages[index])
					index += 1
			units.append(unit)
		return units

	def _fit_payload_context(self, payload: dict) -> dict:
		messages = list(payload.get("messages") or [])
		if not messages:
			return payload
		fixed = [messages[0]] if messages[0].get("role") == "system" else []
		conversation = messages[len(fixed):]
		reserved = max(1, int(payload.get("max_completion_tokens") or self.settings.max_completion_tokens))
		overhead = self._estimate_tokens({
			"model": payload.get("model"),
			"tools": payload.get("tools") or [],
			"tool_choice": payload.get("tool_choice"),
		})
		available = self.settings.max_context_tokens - reserved - overhead
		fixed_cost = sum(self._estimate_tokens(message) for message in fixed)
		available -= fixed_cost
		if available <= 0:
			raise RuntimeError("System prompt exceeds the configured context-token budget")
		units = self._message_units(conversation)
		selected: list[list[dict]] = []
		used = 0
		for unit in reversed(units):
			cost = sum(self._estimate_tokens(message) for message in unit)
			if selected and used + cost > available:
				continue
			if not selected and cost > available:
				raise RuntimeError("Latest conversation turn exceeds the context-token budget")
			selected.append(unit)
			used += cost
		selected.reverse()
		payload["messages"] = [*fixed, *(message for unit in selected for message in unit)]
		return payload

	def _build_payload(self, request: ChatRequest) -> tuple[dict, str, ChatRequest]:
		if not self.settings.litellm_api_key:
			raise RuntimeError("MYAPP_AI_LITELLM_API_KEY is not configured")

		request = with_effective_prompt(request)
		prompt_spec = get_prompt_spec(request.scenario)
		trace_id = uuid.uuid4().hex
		end_user_id = hashlib.sha256(f"myapp-ai:{request.user}".encode("utf-8")).hexdigest()
		context_lines = [f"场景：{request.scenario}", f"Prompt 版本：{request.prompt_version}"]
		if request.company:
			context_lines.append(f"当前公司上下文：{request.company}")
		if request.context:
			agent_state = (
				request.context.get("conversation_state")
				if isinstance(request, AgentRequest) and isinstance(request.context, dict)
				else None
			)
			context_value = agent_state if isinstance(agent_state, dict) else request.context
			context_json = json.dumps(context_value, ensure_ascii=False, separators=(",", ":"))
			if len(context_json) > 30000:
				raise RuntimeError("Business context is too large")
			if isinstance(agent_state, dict):
				context_lines.extend(
					[
						"以下 <conversation_state> 是服务端白名单裁剪后的实体引用状态，只能用于消解当前消息中的省略和指代；任何状态、金额或业务事实仍必须调用工具重新查询：",
						f"<conversation_state>{context_json}</conversation_state>",
					]
				)
				resolved_references = []
				for slot, entity in (agent_state.get("active_entities") or {}).items():
					if not isinstance(entity, dict) or entity.get("resolution_status") != "resolved":
						continue
					if not entity.get("entity_type") or not entity.get("entity_id"):
						continue
					resolved_references.append({
						"slot": slot,
						"entity_type": entity["entity_type"],
						"entity_id": entity["entity_id"],
					})
				if resolved_references:
					context_lines.extend(
						[
							"当前消息若使用“这个/刚才那个/该”等指代并与下列唯一 resolved 引用匹配，不得再次要求用户提供 ID；必须把该 ID 传给对应只读工具重新查询：",
							f"<resolved_entity_references>{json.dumps(resolved_references, ensure_ascii=False, separators=(',', ':'))}</resolved_entity_references>",
						]
					)
			elif request.scenario == "intent_parse":
				context_lines.extend(
					[
						"以下 <conversation_state> 是服务端裁剪后的会话工作状态，只能用于消解当前消息的省略和指代：",
						f"<conversation_state>{context_json}</conversation_state>",
					]
				)
			else:
				context_lines.extend(
					[
						"以下 <business_context> 仅包含当前账号权限与公司范围内的服务端受控业务查询结果：",
						f"<business_context>{context_json}</business_context>",
					]
				)

		payload = {
			"model": self.settings.model,
			"messages": [
				{"role": "system", "content": f"{prompt_spec.text}\n" + "\n".join(context_lines)},
				*self._provider_messages(request),
			],
			"max_completion_tokens": self.settings.max_completion_tokens,
			"user": f"myapp-{end_user_id}",
		}
		if self.settings.reasoning_effort and self.settings.reasoning_effort.strip().lower() != "none":
			payload["reasoning_effort"] = self.settings.reasoning_effort
		return self._fit_payload_context(payload), trace_id, request

	def _warnings(self, request: ChatRequest) -> list[str]:
		warnings = ["本次回答使用受控查询能力；任何业务写操作都必须由用户在正式业务页面确认。"]
		if not request.context:
			warnings.append("当前回答未使用真实业务数据工具。")
		return warnings

	@staticmethod
	def _usage(usage: dict) -> TokenUsage:
		completion_details = usage.get("completion_tokens_details") or {}
		return TokenUsage(
			prompt_tokens=int(usage.get("prompt_tokens") or 0),
			completion_tokens=int(usage.get("completion_tokens") or 0),
			total_tokens=int(usage.get("total_tokens") or 0),
			reasoning_tokens=int(completion_details.get("reasoning_tokens") or 0),
		)

	def chat(self, request: ChatRequest) -> ChatResponse:
		payload, trace_id, request = self._build_payload(request)
		generation_id = str(uuid.uuid4())
		started_at = utc_now()

		try:
			with httpx.Client(
				base_url=self.settings.litellm_base_url,
				timeout=self.settings.timeout_seconds,
				transport=self.transport,
			) as client:
				response = client.post(
					"/v1/chat/completions",
					headers={
						"Authorization": f"Bearer {self.settings.litellm_api_key}",
						"Content-Type": "application/json",
						"X-MyApp-Trace-Id": trace_id,
					},
					json=payload,
				)
				response.raise_for_status()
				body = response.json()
		except Exception as error:
			self.langfuse.record_generation(
				request=request,
				trace_id=trace_id,
				generation_id=generation_id,
				started_at=started_at,
				completed_at=utc_now(),
				model=self.settings.model,
				model_alias=self.settings.model,
				output="",
				usage=TokenUsage(),
				error=type(error).__name__,
			)
			raise

		choice = (body.get("choices") or [{}])[0]
		content = ((choice.get("message") or {}).get("content") or "").strip()
		if not content:
			self.langfuse.record_generation(
				request=request,
				trace_id=trace_id,
				generation_id=generation_id,
				started_at=started_at,
				completed_at=utc_now(),
				model=str(body.get("model") or self.settings.model),
				model_alias=self.settings.model,
				output="",
				usage=self._usage(body.get("usage") or {}),
				error="EmptyModelResponse",
			)
			raise RuntimeError("AI model returned an empty response")

		result = ChatResponse(
			message=ChatMessage(role="assistant", content=content),
			model=str(body.get("model") or self.settings.model),
			model_alias=self.settings.model,
			trace_id=trace_id,
			usage=self._usage(body.get("usage") or {}),
			warnings=self._warnings(request),
		)
		self.langfuse.record_generation(
			request=request,
			trace_id=trace_id,
			generation_id=generation_id,
			started_at=started_at,
			completed_at=utc_now(),
			model=result.model,
			model_alias=result.model_alias,
			output=content,
			usage=result.usage,
		)
		return result

	def stream(self, request: ChatRequest):
		payload, trace_id, request = self._build_payload(request)
		generation_id = str(uuid.uuid4())
		started_at = utc_now()
		payload.update({"stream": True, "stream_options": {"include_usage": True}})
		content_parts = []
		model = self.settings.model
		usage = TokenUsage()

		yield {"type": "started", "trace_id": trace_id, "model_alias": self.settings.model}
		try:
			with httpx.Client(
				base_url=self.settings.litellm_base_url,
				timeout=self.settings.timeout_seconds,
				transport=self.transport,
			) as client:
				with client.stream(
				"POST",
				"/v1/chat/completions",
				headers={
					"Authorization": f"Bearer {self.settings.litellm_api_key}",
					"Content-Type": "application/json",
					"X-MyApp-Trace-Id": trace_id,
				},
				json=payload,
				) as response:
					response.raise_for_status()
					for line in response.iter_lines():
						if not line or line.startswith(":") or not line.startswith("data:"):
							continue
						data = line[5:].strip()
						if data == "[DONE]":
							break
						chunk = json.loads(data)
						model = str(chunk.get("model") or model)
						if chunk.get("usage"):
							usage = self._usage(chunk["usage"])
						choice = (chunk.get("choices") or [{}])[0]
						delta = (choice.get("delta") or {}).get("content") or ""
						if delta:
							content_parts.append(delta)
							yield {"type": "message_delta", "delta": delta}
		except Exception as error:
			self.langfuse.record_generation(
				request=request,
				trace_id=trace_id,
				generation_id=generation_id,
				started_at=started_at,
				completed_at=utc_now(),
				model=model,
				model_alias=self.settings.model,
				output="".join(content_parts),
				usage=usage,
				error=type(error).__name__,
			)
			raise

		content = "".join(content_parts).strip()
		if not content:
			self.langfuse.record_generation(
				request=request,
				trace_id=trace_id,
				generation_id=generation_id,
				started_at=started_at,
				completed_at=utc_now(),
				model=model,
				model_alias=self.settings.model,
				output="",
				usage=usage,
				error="EmptyModelResponse",
			)
			raise RuntimeError("AI model returned an empty streamed response")
		self.langfuse.record_generation(
			request=request,
			trace_id=trace_id,
			generation_id=generation_id,
			started_at=started_at,
			completed_at=utc_now(),
			model=model,
			model_alias=self.settings.model,
			output=content,
			usage=usage,
		)
		for warning in self._warnings(request):
			yield {"type": "warning", "message": warning}
		yield {
			"type": "completed",
			"message": {"role": "assistant", "content": content},
			"model": model,
			"model_alias": self.settings.model,
			"trace_id": trace_id,
			"usage": usage.model_dump(),
			"warnings": self._warnings(request),
		}

	def _build_structured(
		self, request: ChatRequest, *, scenario: str, schema_class, response_class,
		max_completion_tokens: int, warning: str,
	):
		request = with_effective_prompt(request, scenario=scenario)
		payload, trace_id, request = self._build_payload(request)
		generation_id = str(uuid.uuid4())
		started_at = utc_now()
		payload["max_completion_tokens"] = max_completion_tokens
		payload["response_format"] = {
			"type": "json_schema",
			"json_schema": {"name": scenario, "strict": True, "schema": _strict_response_schema(schema_class)},
		}
		try:
			with httpx.Client(
				base_url=self.settings.litellm_base_url,
				timeout=self.settings.timeout_seconds,
				transport=self.transport,
			) as client:
				response = client.post(
					"/v1/chat/completions",
					headers={
						"Authorization": f"Bearer {self.settings.litellm_api_key}",
						"Content-Type": "application/json", "X-MyApp-Trace-Id": trace_id,
					},
					json=payload,
				)
				response.raise_for_status()
				body = response.json()
			content = str((((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "")).strip()
			if content.startswith("```"):
				content = content.strip("`").removeprefix("json").strip()
			candidate = schema_class.model_validate_json(content)
		except Exception as error:
			self.langfuse.record_generation(
				request=request, trace_id=trace_id, generation_id=generation_id,
				started_at=started_at, completed_at=utc_now(), model=self.settings.model,
				model_alias=self.settings.model, output="", usage=TokenUsage(), error=type(error).__name__,
			)
			raise
		usage = self._usage(body.get("usage") or {})
		model = str(body.get("model") or self.settings.model)
		self.langfuse.record_generation(
			request=request, trace_id=trace_id, generation_id=generation_id,
			started_at=started_at, completed_at=utc_now(), model=model,
			model_alias=self.settings.model, output=candidate.model_dump_json(), usage=usage,
		)
		return response_class(
			**({"intent": candidate} if scenario == "intent_parse" else {"draft": candidate}),
			model=model, model_alias=self.settings.model, trace_id=trace_id, usage=usage, warnings=[warning],
		)

	def parse_intent(self, request: ChatRequest) -> IntentParseResponse:
		return self._build_structured(
			request, scenario="intent_parse", schema_class=IntentParseCandidate,
			response_class=IntentParseResponse, max_completion_tokens=4096,
			warning="本次仅解析用户意图，业务事实仍由后端受控服务查询。",
		)

	def build_sales_order_draft(self, request: ChatRequest) -> SalesOrderDraftResponse:
		request = with_effective_prompt(request, scenario="sales_order_draft")
		payload, trace_id, request = self._build_payload(request)
		generation_id = str(uuid.uuid4())
		started_at = utc_now()
		payload["max_completion_tokens"] = 1600
		payload["response_format"] = {
			"type": "json_schema",
			"json_schema": {
				"name": "sales_order_draft",
				"strict": True,
				"schema": _strict_response_schema(SalesOrderDraftCandidate),
			},
		}
		def execute(model_payload: dict):
			with httpx.Client(
				base_url=self.settings.litellm_base_url,
				timeout=self.settings.timeout_seconds,
				transport=self.transport,
			) as client:
				provider_response = client.post(
					"/v1/chat/completions",
					headers={
						"Authorization": f"Bearer {self.settings.litellm_api_key}",
						"Content-Type": "application/json",
						"X-MyApp-Trace-Id": trace_id,
					},
					json=model_payload,
				)
				provider_response.raise_for_status()
				return provider_response.json()

		try:
			try:
				body = execute(payload)
			except httpx.HTTPStatusError as schema_error:
				if schema_error.response.status_code != 400:
					raise
				fallback_payload = json.loads(json.dumps(payload))
				fallback_payload.pop("response_format", None)
				fallback_payload["messages"][0]["content"] = (
					f"{payload['messages'][0]['content']}\n只返回 JSON 对象，不要 Markdown。必须通过以下 Schema 校验："
					f"{json.dumps(_strict_response_schema(SalesOrderDraftCandidate), ensure_ascii=False)}"
				)
				body = execute(fallback_payload)
			content = str((((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "")).strip()
			if content.startswith("```"):
				content = content.strip("`").removeprefix("json").strip()
			if not content.startswith("{") and "{" in content and "}" in content:
				content = content[content.find("{") : content.rfind("}") + 1]
			draft = SalesOrderDraftCandidate.model_validate_json(content)
		except Exception as error:
			self.langfuse.record_generation(
				request=request, trace_id=trace_id, generation_id=generation_id,
				started_at=started_at, completed_at=utc_now(), model=self.settings.model,
				model_alias=self.settings.model, output="", usage=TokenUsage(), error=type(error).__name__,
			)
			raise
		usage = self._usage(body.get("usage") or {})
		self.langfuse.record_generation(
			request=request, trace_id=trace_id, generation_id=generation_id,
			started_at=started_at, completed_at=utc_now(),
			model=str(body.get("model") or self.settings.model), model_alias=self.settings.model,
			output=draft.model_dump_json(), usage=usage,
		)
		return SalesOrderDraftResponse(
			draft=draft,
			model=str(body.get("model") or self.settings.model),
			model_alias=self.settings.model,
			trace_id=trace_id,
			usage=usage,
			warnings=["当前仅生成销售订单草稿候选，正式订单必须由用户确认创建。"],
		)

	def build_product_setup_draft(self, request: ChatRequest) -> ProductSetupDraftResponse:
		request = with_effective_prompt(request, scenario="product_setup_draft")
		payload, trace_id, request = self._build_payload(request)
		generation_id = str(uuid.uuid4())
		started_at = utc_now()
		payload["max_completion_tokens"] = 4096
		payload["response_format"] = {
			"type": "json_schema",
			"json_schema": {
				"name": "product_setup_draft",
				"strict": True,
				"schema": _strict_response_schema(ProductSetupDraftCandidate),
			},
		}

		def execute(model_payload: dict):
			with httpx.Client(
				base_url=self.settings.litellm_base_url,
				timeout=self.settings.timeout_seconds,
				transport=self.transport,
			) as client:
				response = client.post(
					"/v1/chat/completions",
					headers={
						"Authorization": f"Bearer {self.settings.litellm_api_key}",
						"Content-Type": "application/json",
						"X-MyApp-Trace-Id": trace_id,
					},
					json=model_payload,
				)
				response.raise_for_status()
				return response.json()

		try:
			try:
				body = execute(payload)
			except httpx.HTTPStatusError as schema_error:
				if schema_error.response.status_code != 400:
					raise
				fallback = json.loads(json.dumps(payload))
				fallback.pop("response_format", None)
				fallback["messages"][0]["content"] = (
					f"{payload['messages'][0]['content']}\n只返回 JSON 对象，不要 Markdown。必须通过以下 Schema 校验："
					f"{json.dumps(_strict_response_schema(ProductSetupDraftCandidate), ensure_ascii=False)}"
				)
				body = execute(fallback)
			content = str((((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "")).strip()
			if content.startswith("```"):
				content = content.strip("`").removeprefix("json").strip()
			if not content.startswith("{") and "{" in content and "}" in content:
				content = content[content.find("{") : content.rfind("}") + 1]
			draft = ProductSetupDraftCandidate.model_validate_json(content)
		except Exception as error:
			self.langfuse.record_generation(
				request=request, trace_id=trace_id, generation_id=generation_id,
				started_at=started_at, completed_at=utc_now(), model=self.settings.model,
				model_alias=self.settings.model, output="", usage=TokenUsage(), error=type(error).__name__,
			)
			raise
		usage = self._usage(body.get("usage") or {})
		model = str(body.get("model") or self.settings.model)
		self.langfuse.record_generation(
			request=request, trace_id=trace_id, generation_id=generation_id,
			started_at=started_at, completed_at=utc_now(), model=model,
			model_alias=self.settings.model, output=draft.model_dump_json(), usage=usage,
		)
		return ProductSetupDraftResponse(
			draft=draft, model=model, model_alias=self.settings.model,
			trace_id=trace_id, usage=usage,
			warnings=["当前仅生成商品建档草稿候选，正式商品、价格和初始库存必须由用户在商品页面确认创建。"],
		)

	def build_purchase_order_draft(self, request: ChatRequest) -> PurchaseOrderDraftResponse:
		request = with_effective_prompt(request, scenario="purchase_order_draft")
		payload, trace_id, request = self._build_payload(request)
		generation_id = str(uuid.uuid4())
		started_at = utc_now()
		payload["max_completion_tokens"] = 1600
		payload["response_format"] = {
			"type": "json_schema",
			"json_schema": {
				"name": "purchase_order_draft",
				"strict": True,
				"schema": _strict_response_schema(PurchaseOrderDraftCandidate),
			},
		}
		def execute(model_payload: dict):
			with httpx.Client(base_url=self.settings.litellm_base_url, timeout=self.settings.timeout_seconds, transport=self.transport) as client:
				response = client.post(
					"/v1/chat/completions",
					headers={"Authorization": f"Bearer {self.settings.litellm_api_key}", "Content-Type": "application/json", "X-MyApp-Trace-Id": trace_id},
					json=model_payload,
				)
				response.raise_for_status()
				return response.json()
		try:
			try:
				body = execute(payload)
			except httpx.HTTPStatusError as schema_error:
				if schema_error.response.status_code != 400:
					raise
				fallback = json.loads(json.dumps(payload))
				fallback.pop("response_format", None)
				fallback["messages"][0]["content"] = (
					f"{payload['messages'][0]['content']}\n只返回 JSON 对象，不要 Markdown。必须通过以下 Schema 校验："
					f"{json.dumps(_strict_response_schema(PurchaseOrderDraftCandidate), ensure_ascii=False)}"
				)
				body = execute(fallback)
			content = str((((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "")).strip()
			if content.startswith("```"):
				content = content.strip("`").removeprefix("json").strip()
			if not content.startswith("{") and "{" in content and "}" in content:
				content = content[content.find("{") : content.rfind("}") + 1]
			draft = PurchaseOrderDraftCandidate.model_validate_json(content)
		except Exception as error:
			self.langfuse.record_generation(
				request=request, trace_id=trace_id, generation_id=generation_id, started_at=started_at,
				completed_at=utc_now(), model=self.settings.model, model_alias=self.settings.model,
				output="", usage=TokenUsage(), error=type(error).__name__,
			)
			raise
		usage = self._usage(body.get("usage") or {})
		self.langfuse.record_generation(
			request=request, trace_id=trace_id, generation_id=generation_id, started_at=started_at,
			completed_at=utc_now(), model=str(body.get("model") or self.settings.model),
			model_alias=self.settings.model, output=draft.model_dump_json(), usage=usage,
		)
		return PurchaseOrderDraftResponse(
			draft=draft, model=str(body.get("model") or self.settings.model), model_alias=self.settings.model,
			trace_id=trace_id, usage=usage,
			warnings=["当前仅生成采购订单草稿候选，正式采购单必须由用户确认创建。"],
		)

	def build_inventory_adjustment_draft(self, request: ChatRequest) -> InventoryAdjustmentDraftResponse:
		request = with_effective_prompt(request, scenario="inventory_adjustment_draft")
		payload, trace_id, request = self._build_payload(request)
		generation_id = str(uuid.uuid4())
		started_at = utc_now()
		payload["max_completion_tokens"] = 1000
		payload["response_format"] = {
			"type": "json_schema",
			"json_schema": {
				"name": "inventory_adjustment_draft",
				"strict": True,
				"schema": _strict_response_schema(InventoryAdjustmentDraftCandidate),
			},
		}

		def execute(model_payload: dict):
			with httpx.Client(
				base_url=self.settings.litellm_base_url,
				timeout=self.settings.timeout_seconds,
				transport=self.transport,
			) as client:
				response = client.post(
					"/v1/chat/completions",
					headers={
						"Authorization": f"Bearer {self.settings.litellm_api_key}",
						"Content-Type": "application/json",
						"X-MyApp-Trace-Id": trace_id,
					},
					json=model_payload,
				)
				response.raise_for_status()
				return response.json()

		try:
			try:
				body = execute(payload)
			except httpx.HTTPStatusError as schema_error:
				if schema_error.response.status_code != 400:
					raise
				fallback = json.loads(json.dumps(payload))
				fallback.pop("response_format", None)
				fallback["messages"][0]["content"] = (
					f"{payload['messages'][0]['content']}\n只返回 JSON 对象，不要 Markdown。必须通过以下 Schema 校验："
					f"{json.dumps(_strict_response_schema(InventoryAdjustmentDraftCandidate), ensure_ascii=False)}"
				)
				body = execute(fallback)
			content = str((((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "")).strip()
			if content.startswith("```"):
				content = content.strip("`").removeprefix("json").strip()
			if not content.startswith("{") and "{" in content and "}" in content:
				content = content[content.find("{") : content.rfind("}") + 1]
			draft = InventoryAdjustmentDraftCandidate.model_validate_json(content)
		except Exception as error:
			self.langfuse.record_generation(
				request=request,
				trace_id=trace_id,
				generation_id=generation_id,
				started_at=started_at,
				completed_at=utc_now(),
				model=self.settings.model,
				model_alias=self.settings.model,
				output="",
				usage=TokenUsage(),
				error=type(error).__name__,
			)
			raise
		usage = self._usage(body.get("usage") or {})
		self.langfuse.record_generation(
			request=request,
			trace_id=trace_id,
			generation_id=generation_id,
			started_at=started_at,
			completed_at=utc_now(),
			model=str(body.get("model") or self.settings.model),
			model_alias=self.settings.model,
			output=draft.model_dump_json(),
			usage=usage,
		)
		return InventoryAdjustmentDraftResponse(
			draft=draft,
			model=str(body.get("model") or self.settings.model),
			model_alias=self.settings.model,
			trace_id=trace_id,
			usage=usage,
			warnings=["当前仅生成库存调整草稿候选，正式库存调整必须由用户在库存编辑器中确认提交。"],
		)

	async def _arecord_generation(self, **kwargs) -> None:
		method = getattr(self.langfuse, "arecord_generation", None)
		if method:
			await method(**kwargs)

	@staticmethod
	def _is_transient_provider_error(error: Exception) -> bool:
		if isinstance(error, (httpx.TimeoutException, httpx.NetworkError)):
			return True
		if isinstance(error, httpx.HTTPStatusError):
			status = error.response.status_code
			return status in {408, 425, 429} or status >= 500
		return False

	async def _provider_retry_wait(self, attempt: int) -> None:
		delay = self.settings.provider_retry_backoff_seconds * (2 ** max(0, attempt - 1))
		if delay > 0:
			await asyncio.sleep(delay)

	async def _apost_chat(self, payload: dict, trace_id: str) -> dict:
		if not self.async_client:
			raise RuntimeError("Shared LiteLLM AsyncClient is not configured")
		for attempt in range(1, self.settings.provider_max_attempts + 1):
			try:
				response = await self.async_client.post(
					"/v1/chat/completions",
					headers={
						"Authorization": f"Bearer {self.settings.litellm_api_key}",
						"Content-Type": "application/json",
						"X-MyApp-Trace-Id": trace_id,
					},
					json=payload,
				)
				response.raise_for_status()
				return response.json()
			except Exception as error:
				if (
					attempt >= self.settings.provider_max_attempts
					or not self._is_transient_provider_error(error)
				):
					raise
				await self._provider_retry_wait(attempt)
		raise RuntimeError("Provider retry loop ended without a response")

	async def achat(self, request: ChatRequest) -> ChatResponse:
		payload, trace_id, request = self._build_payload(request)
		generation_id = str(uuid.uuid4())
		started_at = utc_now()
		try:
			body = await self._apost_chat(payload, trace_id)
		except Exception as error:
			await self._arecord_generation(
				request=request, trace_id=trace_id, generation_id=generation_id,
				started_at=started_at, completed_at=utc_now(), model=self.settings.model,
				model_alias=self.settings.model, output="", usage=TokenUsage(), error=type(error).__name__,
			)
			raise
		choice = (body.get("choices") or [{}])[0]
		content = ((choice.get("message") or {}).get("content") or "").strip()
		usage = self._usage(body.get("usage") or {})
		model = str(body.get("model") or self.settings.model)
		if not content:
			await self._arecord_generation(
				request=request, trace_id=trace_id, generation_id=generation_id,
				started_at=started_at, completed_at=utc_now(), model=model,
				model_alias=self.settings.model, output="", usage=usage, error="EmptyModelResponse",
			)
			raise RuntimeError("AI model returned an empty response")
		result = ChatResponse(
			message=ChatMessage(role="assistant", content=content), model=model,
			model_alias=self.settings.model, trace_id=trace_id, usage=usage,
			warnings=self._warnings(request),
		)
		await self._arecord_generation(
			request=request, trace_id=trace_id, generation_id=generation_id,
			started_at=started_at, completed_at=utc_now(), model=model,
			model_alias=self.settings.model, output=content, usage=usage,
		)
		return result

	async def astream(self, request: ChatRequest):
		if not self.async_client:
			raise RuntimeError("Shared LiteLLM AsyncClient is not configured")
		payload, trace_id, request = self._build_payload(request)
		generation_id = str(uuid.uuid4())
		started_at = utc_now()
		payload.update({"stream": True, "stream_options": {"include_usage": True}})
		content_parts = []
		model = self.settings.model
		usage = TokenUsage()
		yield {"type": "started", "trace_id": trace_id, "model_alias": self.settings.model}
		try:
			async with self.async_client.stream(
				"POST", "/v1/chat/completions",
				headers={
					"Authorization": f"Bearer {self.settings.litellm_api_key}",
					"Content-Type": "application/json", "X-MyApp-Trace-Id": trace_id,
				},
				json=payload,
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
						usage = self._usage(chunk["usage"])
					choice = (chunk.get("choices") or [{}])[0]
					delta = (choice.get("delta") or {}).get("content") or ""
					if delta:
						content_parts.append(delta)
						yield {"type": "message_delta", "delta": delta}
		except Exception as error:
			await self._arecord_generation(
				request=request, trace_id=trace_id, generation_id=generation_id,
				started_at=started_at, completed_at=utc_now(), model=model,
				model_alias=self.settings.model, output="".join(content_parts), usage=usage,
				error=type(error).__name__,
			)
			raise
		content = "".join(content_parts).strip()
		if not content:
			await self._arecord_generation(
				request=request, trace_id=trace_id, generation_id=generation_id,
				started_at=started_at, completed_at=utc_now(), model=model,
				model_alias=self.settings.model, output="", usage=usage, error="EmptyModelResponse",
			)
			raise RuntimeError("AI model returned an empty streamed response")
		await self._arecord_generation(
			request=request, trace_id=trace_id, generation_id=generation_id,
			started_at=started_at, completed_at=utc_now(), model=model,
			model_alias=self.settings.model, output=content, usage=usage,
		)
		for warning in self._warnings(request):
			yield {"type": "warning", "message": warning}
		yield {
			"type": "completed", "message": {"role": "assistant", "content": content},
			"model": model, "model_alias": self.settings.model, "trace_id": trace_id,
			"usage": usage.model_dump(), "warnings": self._warnings(request),
		}

	async def _abuild_structured(
		self, request: ChatRequest, *, scenario: str, schema_class, response_class,
		max_completion_tokens: int, warning: str,
	):
		request = with_effective_prompt(request, scenario=scenario)
		payload, trace_id, request = self._build_payload(request)
		generation_id = str(uuid.uuid4())
		started_at = utc_now()
		payload["max_completion_tokens"] = max_completion_tokens
		payload["response_format"] = {
			"type": "json_schema",
			"json_schema": {"name": scenario, "strict": True, "schema": _strict_response_schema(schema_class)},
		}
		try:
			try:
				body = await self._apost_chat(payload, trace_id)
			except httpx.HTTPStatusError as schema_error:
				if schema_error.response.status_code != 400:
					raise
				fallback = json.loads(json.dumps(payload))
				fallback.pop("response_format", None)
				fallback["messages"][0]["content"] = (
					f"{payload['messages'][0]['content']}\n只返回 JSON 对象，不要 Markdown。必须通过以下 Schema 校验："
					f"{json.dumps(_strict_response_schema(schema_class), ensure_ascii=False)}"
				)
				body = await self._apost_chat(fallback, trace_id)
			content = str((((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "")).strip()
			if content.startswith("```"):
				content = content.strip("`").removeprefix("json").strip()
			if not content.startswith("{") and "{" in content and "}" in content:
				content = content[content.find("{") : content.rfind("}") + 1]
			draft = schema_class.model_validate_json(content)
		except Exception as error:
			await self._arecord_generation(
				request=request, trace_id=trace_id, generation_id=generation_id,
				started_at=started_at, completed_at=utc_now(), model=self.settings.model,
				model_alias=self.settings.model, output="", usage=TokenUsage(), error=type(error).__name__,
			)
			raise
		usage = self._usage(body.get("usage") or {})
		model = str(body.get("model") or self.settings.model)
		await self._arecord_generation(
			request=request, trace_id=trace_id, generation_id=generation_id,
			started_at=started_at, completed_at=utc_now(), model=model,
			model_alias=self.settings.model, output=draft.model_dump_json(), usage=usage,
		)
		return response_class(
			**({"intent": draft} if scenario == "intent_parse" else {"draft": draft}),
			model=model, model_alias=self.settings.model,
			trace_id=trace_id, usage=usage, warnings=[warning],
		)

	async def abuild_sales_order_draft(self, request: ChatRequest) -> SalesOrderDraftResponse:
		return await self._abuild_structured(
			request, scenario="sales_order_draft", schema_class=SalesOrderDraftCandidate,
			response_class=SalesOrderDraftResponse, max_completion_tokens=1600,
			warning="当前仅生成销售订单草稿候选，正式订单必须由用户确认创建。",
		)

	async def aparse_intent(self, request: ChatRequest) -> IntentParseResponse:
		return await self._abuild_structured(
			request, scenario="intent_parse", schema_class=IntentParseCandidate,
			response_class=IntentParseResponse, max_completion_tokens=4096,
			warning="本次仅解析用户意图，业务事实仍由后端受控服务查询。",
		)

	async def abuild_purchase_order_draft(self, request: ChatRequest) -> PurchaseOrderDraftResponse:
		return await self._abuild_structured(
			request, scenario="purchase_order_draft", schema_class=PurchaseOrderDraftCandidate,
			response_class=PurchaseOrderDraftResponse, max_completion_tokens=1600,
			warning="当前仅生成采购订单草稿候选，正式采购单必须由用户确认创建。",
		)

	async def abuild_inventory_adjustment_draft(self, request: ChatRequest) -> InventoryAdjustmentDraftResponse:
		return await self._abuild_structured(
			request, scenario="inventory_adjustment_draft", schema_class=InventoryAdjustmentDraftCandidate,
			response_class=InventoryAdjustmentDraftResponse, max_completion_tokens=1000,
			warning="当前仅生成库存调整草稿候选，正式库存调整必须由用户在库存编辑器中确认提交。",
		)

	async def abuild_product_setup_draft(self, request: ChatRequest) -> ProductSetupDraftResponse:
		return await self._abuild_structured(
			request, scenario="product_setup_draft", schema_class=ProductSetupDraftCandidate,
			response_class=ProductSetupDraftResponse, max_completion_tokens=4096,
			warning="当前仅生成商品建档草稿候选，正式商品、价格和初始库存必须由用户在商品页面确认创建。",
		)
