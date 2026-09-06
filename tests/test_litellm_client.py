import base64
import hashlib
import json
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

import httpx

from myapp_ai.config import Settings
from myapp_ai.litellm_client import LiteLLMClient, _strict_response_schema
from myapp_ai.schemas import (
	ChatMessage,
	ChatRequest,
	ImageAttachment,
	IntentParseCandidate,
	InventoryAdjustmentDraftCandidate,
	ProductSetupDraftCandidate,
	PurchaseOrderDraftCandidate,
	SalesOrderDraftCandidate,
)


def _sales_order_v5_create(
	*, customer: str | None, lines: list[dict], transaction_date: str | None = None,
	delivery_date: str | None = None, sales_mode: str | None = None,
	warehouse: str | None = None, remarks: str | None = None,
) -> dict:
	return {
		"operation": "create",
		"target": {"order_number": None, "context_ref": None},
		"header_patch": {
			"customer_query": customer, "transaction_date": transaction_date,
			"delivery_date": delivery_date, "default_sales_mode": sales_mode,
			"warehouse_query": warehouse, "remarks": remarks, "clear_fields": [],
		},
		"line_update_mode": "patch",
		"line_changes": [
			{
				"operation": "add",
				"target": {"row_id": None, "item_query": None, "context_ref": None},
				"patch": {
					"replacement_item_query": line["item_query"], "qty": line.get("qty"),
					"uom": line.get("uom"), "price": line.get("price"),
					"warehouse_query": line.get("warehouse_query"),
					"specification_query": None,
				},
				"evidence": [],
			}
			for line in lines
		],
		"order_number": None, "source_document_type": "unstructured",
		"customer_query": None, "transaction_date": None, "delivery_date": None,
		"default_sales_mode": None, "warehouse_query": None, "remarks": None,
		"items": [], "evidence": [],
	}


def _purchase_order_v5_create(
	*, supplier: str | None, lines: list[dict], transaction_date: str | None = None,
	schedule_date: str | None = None, purchase_mode: str | None = None,
	warehouse: str | None = None, currency: str | None = None,
	supplier_ref: str | None = None, remarks: str | None = None,
) -> dict:
	return {
		"operation": "create",
		"target": {"order_number": None, "context_ref": None},
		"header_patch": {
			"supplier_query": supplier, "transaction_date": transaction_date,
			"schedule_date": schedule_date, "default_purchase_mode": purchase_mode,
			"warehouse_query": warehouse, "currency": currency,
			"supplier_ref": supplier_ref, "remarks": remarks, "clear_fields": [],
		},
		"line_update_mode": "patch",
		"line_changes": [
			{
				"operation": "add",
				"target": {"row_id": None, "item_query": None, "context_ref": None},
				"patch": {
					"replacement_item_query": line["item_query"], "qty": line.get("qty"),
					"uom": line.get("uom"), "price": line.get("price"),
					"warehouse_query": line.get("warehouse_query"),
					"specification_query": None,
				},
				"evidence": [],
			}
			for line in lines
		],
		"order_number": None, "source_document_type": "unstructured",
		"supplier_query": None, "transaction_date": None, "schedule_date": None,
		"default_purchase_mode": None, "warehouse_query": None, "currency": None,
		"supplier_ref": None, "remarks": None, "items": [], "evidence": [],
	}


class FakeLangfuseClient:
	def __init__(self):
		self.generations = []

	def record_generation(self, **kwargs):
		self.generations.append(kwargs)
		return True


class FakeAsyncLangfuseClient:
	def __init__(self):
		self.generations = []

	async def arecord_generation(self, **kwargs):
		self.generations.append(kwargs)
		return True


class TestLiteLLMClient(TestCase):
	def test_all_structured_response_schemas_are_openai_strict(self):
		def assert_object_nodes_are_strict(node):
			if isinstance(node, dict):
				properties = node.get("properties")
				if isinstance(properties, dict):
					self.assertFalse(node.get("additionalProperties"))
					self.assertEqual(set(node.get("required") or []), set(properties))
				for value in node.values():
					assert_object_nodes_are_strict(value)
			elif isinstance(node, list):
				for value in node:
					assert_object_nodes_are_strict(value)

		for schema_class in (
			IntentParseCandidate,
			SalesOrderDraftCandidate,
			PurchaseOrderDraftCandidate,
			InventoryAdjustmentDraftCandidate,
			ProductSetupDraftCandidate,
		):
			with self.subTest(schema=schema_class.__name__):
				assert_object_nodes_are_strict(_strict_response_schema(schema_class))

	def test_build_payload_places_private_attachment_on_latest_user_message(self):
		content = b"synthetic-image"
		settings = Settings(
			litellm_base_url="http://litellm.test", litellm_api_key="test-key",
			model="erp-vision", reasoning_effort="none", service_token="service-token",
			timeout_seconds=10, max_messages=20, max_message_chars=8000,
		)
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="请识别图片中的商品")],
			attachments=[ImageAttachment(
				attachment_id="AI-ATT-1", filename="item.png", mime_type="image/png",
				sha256=hashlib.sha256(content).hexdigest(),
				data_base64=base64.b64encode(content).decode(),
			)],
			user="test@example.com",
		)

		payload, _trace_id, _request = LiteLLMClient(settings)._build_payload(request)
		self.assertNotIn("reasoning_effort", payload)

		user_content = payload["messages"][-1]["content"]
		self.assertEqual(user_content[0], {"type": "text", "text": "请识别图片中的商品"})
		self.assertEqual(
			user_content[1],
			{"type": "text", "text": '<image_attachment attachment_id="AI-ATT-1" />'},
		)
		self.assertEqual(user_content[2]["type"], "image_url")
		self.assertTrue(user_content[2]["image_url"]["url"].startswith("data:image/png;base64,"))

	def test_reasoning_default_is_omitted_but_explicit_effort_is_preserved(self):
		for effort in ("", "none", " NONE ", "low", "high"):
			with self.subTest(effort=effort):
				settings = Settings(
					litellm_base_url="http://litellm.test", litellm_api_key="test-key",
					model="arbitrary-provider-model", reasoning_effort=effort,
					service_token="service-token", timeout_seconds=10,
					max_messages=20, max_message_chars=8000,
				)
				payload, _, _ = LiteLLMClient(settings)._build_payload(ChatRequest(
					messages=[ChatMessage(role="user", content="hello")], user="tester",
				))
				if effort in {"low", "high"}:
					self.assertEqual(payload["reasoning_effort"], effort)
				else:
					self.assertNotIn("reasoning_effort", payload)

	def test_build_payload_keeps_historical_images_on_their_original_messages(self):
		first = b"first-image"
		second = b"second-image"
		settings = Settings(
			litellm_base_url="http://litellm.test", litellm_api_key="test-key",
			model="erp-vision", reasoning_effort="none", service_token="service-token",
			timeout_seconds=10, max_messages=20, max_message_chars=8000,
		)
		request = ChatRequest(
			messages=[
				ChatMessage(role="user", content="第一张", attachments=[ImageAttachment(
					attachment_id="AI-ATT-1", mime_type="image/png",
					sha256=hashlib.sha256(first).hexdigest(),
					data_base64=base64.b64encode(first).decode(),
				)]),
				ChatMessage(role="assistant", content="已看到第一张"),
				ChatMessage(role="user", content="第二张", attachments=[ImageAttachment(
					attachment_id="AI-ATT-2", mime_type="image/png",
					sha256=hashlib.sha256(second).hexdigest(),
					data_base64=base64.b64encode(second).decode(),
				)]),
			],
			attachments=[ImageAttachment(
				attachment_id="AI-ATT-2", mime_type="image/png",
				sha256=hashlib.sha256(second).hexdigest(),
				data_base64=base64.b64encode(second).decode(),
			)],
			user="test@example.com",
		)

		payload, _trace_id, _request = LiteLLMClient(settings)._build_payload(request)

		provider_messages = payload["messages"][1:]
		self.assertEqual(len(provider_messages[0]["content"]), 3)
		self.assertEqual(provider_messages[1]["content"], "已看到第一张")
		self.assertEqual(len(provider_messages[2]["content"]), 3)
		self.assertIn(base64.b64encode(first).decode(), provider_messages[0]["content"][2]["image_url"]["url"])
		self.assertIn(base64.b64encode(second).decode(), provider_messages[2]["content"][2]["image_url"]["url"])

	def test_payload_context_budget_keeps_latest_turns_and_drops_old_history(self):
		settings = Settings(
			litellm_base_url="http://litellm.test", litellm_api_key="test-key",
			model="erp-chat", reasoning_effort="none", service_token="service-token",
			timeout_seconds=10, max_messages=20, max_message_chars=8000,
			max_context_tokens=1800, max_completion_tokens=100,
		)
		messages = [
			ChatMessage(role="user" if index % 2 == 0 else "assistant", content=f"turn-{index}-" + "x" * 1200)
			for index in range(6)
		]

		payload, _trace_id, _request = LiteLLMClient(settings)._build_payload(ChatRequest(
			messages=messages, user="test@example.com",
		))

		self.assertLess(len(payload["messages"]), len(messages) + 1)
		self.assertTrue(payload["messages"][-1]["content"].startswith("turn-5-"))
		self.assertEqual(payload["messages"][0]["role"], "system")

	def test_build_inventory_adjustment_draft_uses_inventory_schema(self):
		captured = {}

		def handler(request: httpx.Request):
			captured.update(json.loads(request.content))
			return httpx.Response(200, json={
				"model": "structured-model",
				"choices": [{"message": {"content": json.dumps({
					"item_query": "数码相机", "warehouse_query": "Stores - TC",
					"adjustment_type": "set_target", "quantity": 8, "uom": "Nos",
					"posting_date": "2026-07-13", "reason": "盘点差异",
				}, ensure_ascii=False)}}],
				"usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
			})

		settings = Settings(
			litellm_base_url="http://litellm.test", litellm_api_key="test-key", model="erp-structured",
			reasoning_effort="none", service_token="service-token", timeout_seconds=10,
			max_messages=20, max_message_chars=8000,
		)
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="把 Stores - TC 的数码相机库存调整到 8 个，原因是盘点差异")],
			user="test@example.com", scenario="inventory_adjustment_draft",
		)
		result = LiteLLMClient(
			settings, transport=httpx.MockTransport(handler), langfuse_client=FakeLangfuseClient(),
		).build_inventory_adjustment_draft(request)

		self.assertEqual(captured["response_format"]["json_schema"]["name"], "inventory_adjustment_draft")
		self.assertIn("Prompt 版本：inventory-adjustment-draft-v3", captured["messages"][0]["content"])
		self.assertEqual(result.draft.adjustment_type, "set_target")
		self.assertEqual(result.draft.quantity, 8)

	def test_build_purchase_order_draft_uses_purchase_schema(self):
		captured = {}

		def handler(request: httpx.Request):
			captured.update(json.loads(request.content))
			return httpx.Response(200, json={
				"model": "structured-model",
				"choices": [{"message": {"content": json.dumps(_purchase_order_v5_create(
					supplier="供应商A",
					lines=[{"item_query": "相机", "qty": 2, "uom": "Box", "price": None, "warehouse_query": None}],
				), ensure_ascii=False)}}],
				"usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
			})

		settings = Settings(
			litellm_base_url="http://litellm.test", litellm_api_key="test-key", model="erp-structured",
			reasoning_effort="none", service_token="service-token", timeout_seconds=10,
			max_messages=20, max_message_chars=8000,
		)
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="向供应商A采购2箱相机")],
			user="test@example.com", scenario="purchase_order_draft",
		)
		result = LiteLLMClient(
			settings, transport=httpx.MockTransport(handler), langfuse_client=FakeLangfuseClient(),
		).build_purchase_order_draft(request)

		self.assertEqual(captured["response_format"]["json_schema"]["name"], "purchase_order_draft")
		self.assertEqual(result.draft.header_patch.supplier_query, "供应商A")
		self.assertEqual(result.draft.line_changes[0].patch.qty, 2)

	def test_build_sales_order_draft_uses_strict_json_schema(self):
		captured = {}

		def handler(request: httpx.Request):
			captured.update(json.loads(request.content))
			return httpx.Response(
				200,
				json={
					"model": "structured-model",
					"choices": [{"message": {"content": json.dumps(_sales_order_v5_create(
						customer="客户A", sales_mode="wholesale",
						lines=[{"item_query": "相机", "qty": 2, "uom": "Box", "price": None, "warehouse_query": None}],
					), ensure_ascii=False)}}],
					"usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
				},
			)

		settings = Settings(
			litellm_base_url="http://litellm.test", litellm_api_key="test-key",
			model="erp-structured", reasoning_effort="none", service_token="service-token",
			timeout_seconds=10, max_messages=20, max_message_chars=8000,
		)
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="给客户A开2箱相机")],
			user="test@example.com", scenario="sales_order_draft",
			conversation_id="AI-CONV-1", run_id="AI-RUN-1",
		)
		result = LiteLLMClient(
			settings, transport=httpx.MockTransport(handler), langfuse_client=FakeLangfuseClient(),
		).build_sales_order_draft(request)

		self.assertEqual(captured["response_format"]["type"], "json_schema")
		self.assertTrue(captured["response_format"]["json_schema"]["strict"])
		self.assertIn("Prompt 版本：sales-order-draft-v5", captured["messages"][0]["content"])
		mode_schema = captured["response_format"]["json_schema"]["schema"]["properties"]["default_sales_mode"]
		self.assertTrue(any(branch.get("type") == "null" for branch in mode_schema["anyOf"]))
		self.assertEqual(result.draft.header_patch.customer_query, "客户A")
		self.assertEqual(result.draft.line_changes[0].patch.qty, 2)

	def test_chat_uses_configured_model_and_lowest_reasoning(self):
		captured = {}

		def handler(request: httpx.Request):
			captured.update(json.loads(request.content))
			return httpx.Response(
				200,
				json={
					"model": "gpt-5.5",
					"choices": [{"message": {"content": "你好"}, "finish_reason": "stop"}],
					"usage": {
						"prompt_tokens": 10,
						"completion_tokens": 2,
						"total_tokens": 12,
						"completion_tokens_details": {"reasoning_tokens": 0},
					},
				},
			)

		settings = Settings(
			litellm_base_url="http://litellm.test",
			litellm_api_key="test-key",
			model="gpt-5.5",
			reasoning_effort="none",
			service_token="service-token",
			timeout_seconds=10,
			max_messages=20,
			max_message_chars=8000,
		)
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="你好")],
			user="test@example.com",
			context={"products": [{"item_code": "ITEM-001", "item_name": "测试商品"}]},
			prompt_version="erp-readonly-v11",
		)

		langfuse = FakeLangfuseClient()
		result = LiteLLMClient(
			settings,
			transport=httpx.MockTransport(handler),
			langfuse_client=langfuse,
		).chat(request)

		self.assertEqual(captured["model"], "gpt-5.5")
		self.assertNotIn("reasoning_effort", captured)
		self.assertRegex(captured["user"], r"^myapp-[0-9a-f]{64}$")
		self.assertNotIn("test@example.com", json.dumps(captured, ensure_ascii=False))
		self.assertIn("ITEM-001", captured["messages"][0]["content"])
		self.assertIn("erp-readonly-v11", captured["messages"][0]["content"])
		self.assertIn("不要逐条复述记录", captured["messages"][0]["content"])
		self.assertIn("不得声称“结果正常”", captured["messages"][0]["content"])
		self.assertEqual(result.message.content, "你好")
		self.assertEqual(result.usage.reasoning_tokens, 0)
		self.assertEqual(len(result.warnings), 1)
		self.assertEqual(len(langfuse.generations), 1)
		self.assertEqual(langfuse.generations[0]["output"], "你好")
		self.assertEqual(langfuse.generations[0]["request"].prompt_version, "erp-readonly-v11")

	def test_sales_draft_falls_back_from_rejected_json_schema_and_keeps_prompt_version(self):
		captured = []

		def handler(request: httpx.Request):
			payload = json.loads(request.content)
			captured.append(payload)
			if len(captured) == 1:
				return httpx.Response(400, json={"error": "response_format unsupported"})
			return httpx.Response(200, json={
				"model": "fallback-model",
				"choices": [{"message": {"content": json.dumps(_sales_order_v5_create(
					customer="客户A", sales_mode="wholesale",
					lines=[{"item_query": "相机", "qty": 2, "uom": "Box", "price": None, "warehouse_query": None}],
				), ensure_ascii=False)}}],
				"usage": {},
			})

		settings = Settings(
			litellm_base_url="http://litellm.test", litellm_api_key="test-key",
			model="erp-structured", reasoning_effort="none", service_token="service-token",
			timeout_seconds=10, max_messages=20, max_message_chars=8000,
		)
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="给客户A开2箱相机")],
			user="test@example.com", scenario="general",
		)
		langfuse = FakeLangfuseClient()

		result = LiteLLMClient(
			settings, transport=httpx.MockTransport(handler), langfuse_client=langfuse,
		).build_sales_order_draft(request)

		self.assertEqual(len(captured), 2)
		self.assertIn("response_format", captured[0])
		self.assertNotIn("response_format", captured[1])
		self.assertIn("sales-order-draft-v5", captured[1]["messages"][0]["content"])
		self.assertEqual(result.draft.header_patch.customer_query, "客户A")
		self.assertEqual(langfuse.generations[0]["request"].scenario, "sales_order_draft")
		self.assertEqual(langfuse.generations[0]["request"].prompt_version, "sales-order-draft-v5")

	def test_stream_emits_incremental_content_and_completed_metadata(self):
		captured = {}

		def handler(request: httpx.Request):
			captured.update(json.loads(request.content))
			return httpx.Response(
				200,
				text="\n".join(
					[
						'data: {"model":"opencode-deepseek-v4-flash","choices":[{"delta":{"content":"连接"}}]}',
						'data: {"model":"opencode-deepseek-v4-flash","choices":[{"delta":{"content":"成功"}}]}',
						'data: {"model":"opencode-deepseek-v4-flash","choices":[],"usage":{"prompt_tokens":8,"completion_tokens":2,"total_tokens":10,"completion_tokens_details":{"reasoning_tokens":1}}}',
						"data: [DONE]",
					],
				),
			)

		settings = Settings(
			litellm_base_url="http://litellm.test",
			litellm_api_key="test-key",
			model="opencode-deepseek-v4-flash",
			reasoning_effort="",
			service_token="service-token",
			timeout_seconds=10,
			max_messages=20,
			max_message_chars=8000,
		)
		request = ChatRequest(messages=[ChatMessage(role="user", content="你好")], user="test@example.com")

		langfuse = FakeLangfuseClient()
		events = list(
			LiteLLMClient(
				settings,
				transport=httpx.MockTransport(handler),
				langfuse_client=langfuse,
			).stream(request)
		)

		self.assertTrue(captured["stream"])
		self.assertNotIn("reasoning_effort", captured)
		self.assertEqual(
			[event["delta"] for event in events if event["type"] == "message_delta"],
			["连接", "成功"],
		)
		completed = events[-1]
		self.assertEqual(completed["type"], "completed")
		self.assertEqual(completed["message"]["content"], "连接成功")
		self.assertEqual(completed["usage"]["reasoning_tokens"], 1)
		self.assertEqual(len(langfuse.generations), 1)
		self.assertEqual(langfuse.generations[0]["output"], "连接成功")


class TestAsyncLiteLLMClient(IsolatedAsyncioTestCase):
	def _settings(self, *, model: str = "erp-fast-chat") -> Settings:
		return Settings(
			litellm_base_url="http://litellm.test", litellm_api_key="test-key",
			model=model, reasoning_effort="none", service_token="service-token",
			timeout_seconds=10, max_messages=20, max_message_chars=8000,
		)

	async def test_async_chat_and_stream_use_shared_client(self):
		requests = []

		def handler(request: httpx.Request):
			payload = json.loads(request.content)
			requests.append(payload)
			if payload.get("stream"):
				return httpx.Response(200, text="\n".join([
					'data: {"model":"erp-fast-chat","choices":[{"delta":{"content":"异步"}}]}',
					'data: {"model":"erp-fast-chat","choices":[{"delta":{"content":"成功"}}]}',
					'data: {"model":"erp-fast-chat","choices":[],"usage":{"prompt_tokens":4,"completion_tokens":2,"total_tokens":6}}',
					"data: [DONE]",
				]))
			return httpx.Response(200, json={
				"model": "erp-fast-chat", "choices": [{"message": {"content": "你好"}}],
				"usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
			})

		async_client = httpx.AsyncClient(
			base_url="http://litellm.test", transport=httpx.MockTransport(handler),
		)
		langfuse = FakeAsyncLangfuseClient()
		client = LiteLLMClient(
			self._settings(), async_client=async_client, langfuse_client=langfuse,
		)
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="你好")], user="test@example.com",
		)
		try:
			chat = await client.achat(request)
			events = [event async for event in client.astream(request)]
		finally:
			await async_client.aclose()

		self.assertEqual(chat.message.content, "你好")
		self.assertEqual(events[-1]["message"]["content"], "异步成功")
		self.assertEqual(len(requests), 2)
		self.assertIs(client.async_client, async_client)
		self.assertEqual(len(langfuse.generations), 2)

	async def test_async_structured_draft_keeps_schema_fallback(self):
		payloads = []

		def handler(request: httpx.Request):
			payload = json.loads(request.content)
			payloads.append(payload)
			if len(payloads) == 1:
				return httpx.Response(400, json={"error": "response_format unsupported"})
			return httpx.Response(200, json={
				"model": "erp-structured",
				"choices": [{"message": {"content": json.dumps(_sales_order_v5_create(
					customer="客户A", sales_mode="wholesale",
					lines=[{"item_query": "相机", "qty": 2, "uom": "Box", "price": None, "warehouse_query": None}],
				), ensure_ascii=False)}}],
				"usage": {},
			})

		async_client = httpx.AsyncClient(
			base_url="http://litellm.test", transport=httpx.MockTransport(handler),
		)
		langfuse = FakeAsyncLangfuseClient()
		client = LiteLLMClient(
			self._settings(model="erp-structured"),
			async_client=async_client, langfuse_client=langfuse,
		)
		try:
			result = await client.abuild_sales_order_draft(ChatRequest(
				messages=[ChatMessage(role="user", content="给客户A开2箱相机")],
				user="test@example.com", scenario="general",
			))
		finally:
			await async_client.aclose()

		self.assertEqual(result.draft.header_patch.customer_query, "客户A")
		self.assertEqual(len(payloads), 2)
		self.assertIn("response_format", payloads[0])
		self.assertNotIn("response_format", payloads[1])
		self.assertIn("sales-order-draft-v5", payloads[1]["messages"][0]["content"])
		self.assertEqual(langfuse.generations[0]["request"].scenario, "sales_order_draft")

	async def test_async_structured_request_retries_one_transient_provider_error(self):
		requests = []

		def handler(request: httpx.Request):
			requests.append(json.loads(request.content))
			if len(requests) == 1:
				raise httpx.ReadError("transient upstream disconnect", request=request)
			return httpx.Response(200, json={
				"model": "erp-fast-chat",
				"choices": [{"message": {"content": json.dumps({
					"intent": "product_search", "confidence": 0.9,
					"product_query": "可乐", "product_terms": ["可乐"],
					"product_hypotheses": [],
					"product_attributes": {
						"brand": None, "item_group": None, "color": None,
						"flavor": None, "specification": None,
						"capacity": None, "packaging": None,
					},
					"entities": [], "report_type": None, "date_preset": "all",
					"date_from": None, "date_to": None, "status": "all",
					"sort": "latest", "min_amount": None, "limit": 10,
				}, ensure_ascii=False)}}],
				"usage": {},
			})

		async_client = httpx.AsyncClient(
			base_url="http://litellm.test", transport=httpx.MockTransport(handler),
		)
		client = LiteLLMClient(
			self._settings(), async_client=async_client,
			langfuse_client=FakeAsyncLangfuseClient(),
		)
		try:
			with patch("myapp_ai.litellm_client.asyncio.sleep", new=AsyncMock()) as sleep:
				result = await client.aparse_intent(ChatRequest(
					messages=[ChatMessage(role="user", content="查询可乐")],
					user="test@example.com", scenario="intent_parse",
				))
		finally:
			await async_client.aclose()

		self.assertEqual(result.intent.product_query, "可乐")
		self.assertEqual(len(requests), 2)
		sleep.assert_awaited_once()

	async def test_async_intent_parser_uses_strict_schema(self):
		captured = {}

		def handler(request: httpx.Request):
			captured.update(json.loads(request.content))
			return httpx.Response(200, json={
				"model": "erp-fast-chat",
				"choices": [{"message": {"content": json.dumps({
					"intent": "order_query", "confidence": 0.98, "product_query": None,
					"entities": ["sales_order"], "report_type": None,
					"date_preset": "last_30_days", "status": "unfinished",
					"date_from": None, "date_to": None, "min_amount": None,
					"sort": "amount_desc", "limit": 3,
				}, ensure_ascii=False)}}],
				"usage": {},
			})

		async_client = httpx.AsyncClient(
			base_url="http://litellm.test", transport=httpx.MockTransport(handler),
		)
		client = LiteLLMClient(
			self._settings(), async_client=async_client, langfuse_client=FakeAsyncLangfuseClient(),
		)
		try:
			result = await client.aparse_intent(ChatRequest(
				messages=[ChatMessage(role="user", content="最近一个月还没完成的销售订单，列前三张")],
				user="test@example.com", scenario="intent_parse",
				context={"conversation_state": {"active_scenario": "order_query", "order": {"status": "all"}}},
			))
		finally:
			await async_client.aclose()

		self.assertEqual(captured["response_format"]["json_schema"]["name"], "intent_parse")
		self.assertTrue(captured["response_format"]["json_schema"]["strict"])
		self.assertFalse(captured["response_format"]["json_schema"]["schema"]["additionalProperties"])
		self.assertEqual(
			set(captured["response_format"]["json_schema"]["schema"]["required"]),
			set(captured["response_format"]["json_schema"]["schema"]["properties"]),
		)
		self.assertEqual(result.intent.intent, "order_query")
		self.assertEqual(result.intent.status, "unfinished")
		self.assertEqual(result.intent.entities, ["sales_order"])
		self.assertEqual(result.intent.limit, 3)
		self.assertIn("<conversation_state>", captured["messages"][0]["content"])
		self.assertIn("active_scenario", captured["messages"][0]["content"])
		self.assertIn("product_terms", captured["response_format"]["json_schema"]["schema"]["properties"])
		self.assertIn("product_attributes", captured["response_format"]["json_schema"]["schema"]["properties"])

	async def test_async_intent_parser_sends_product_image_to_structured_model(self):
		captured = {}
		content = b"synthetic-cola-image"

		def handler(request: httpx.Request):
			captured.update(json.loads(request.content))
			return httpx.Response(200, json={
				"model": "erp-vision",
				"choices": [{"message": {"content": json.dumps({
					"intent": "product_search", "confidence": 0.98,
					"product_query": "可口可乐", "entities": [], "report_type": None,
					"date_preset": "all", "status": "all", "date_from": None,
					"date_to": None, "min_amount": None, "sort": "latest", "limit": 10,
				}, ensure_ascii=False)}}],
				"usage": {},
			})

		async_client = httpx.AsyncClient(
			base_url="http://litellm.test", transport=httpx.MockTransport(handler),
		)
		client = LiteLLMClient(
			self._settings(), async_client=async_client, langfuse_client=FakeAsyncLangfuseClient(),
		)
		try:
			result = await client.aparse_intent(ChatRequest(
				messages=[ChatMessage(role="user", content="我们的商品中有没有这个商品")],
				attachments=[ImageAttachment(
					attachment_id="AI-ATT-COLA", mime_type="image/webp",
					sha256=hashlib.sha256(content).hexdigest(),
					data_base64=base64.b64encode(content).decode(),
				)],
				user="test@example.com", scenario="intent_parse",
			))
		finally:
			await async_client.aclose()

		user_content = captured["messages"][-1]["content"]
		self.assertEqual(user_content[0]["text"], "我们的商品中有没有这个商品")
		self.assertEqual(
			user_content[1]["text"],
			'<image_attachment attachment_id="AI-ATT-COLA" />',
		)
		self.assertEqual(user_content[2]["type"], "image_url")
		self.assertIn("不能把“这个商品", captured["messages"][0]["content"])
		self.assertEqual(result.intent.product_query, "可口可乐")
		self.assertEqual(result.intent.product_terms, [])

	async def test_async_product_setup_draft_uses_product_schema(self):
		captured = {}

		def handler(request: httpx.Request):
			captured.update(json.loads(request.content))
			return httpx.Response(200, json={
				"model": "erp-structured",
				"choices": [{"message": {"content": json.dumps({
					"operation": "create",
					"target": {
						"item_code": None, "barcode": None, "query": None, "context_ref": None,
					},
					"patch": {
						"item_name": "传承结晶", "new_item_code": None,
						"item_group_query": None, "brand_query": None,
						"stock_uom": None, "warehouse_query": None,
						"opening_qty": 1000, "opening_uom": "个",
						"standard_selling_rate": 9999, "wholesale_rate": 8800,
						"retail_rate": 10800, "standard_buying_rate": 5000,
						"valuation_rate": None, "currency": None, "description": None,
						"barcode": None, "specification": None, "clear_fields": [],
					},
					"evidence": [],
				}, ensure_ascii=False)}}],
				"usage": {},
			})

		async_client = httpx.AsyncClient(
			base_url="http://litellm.test", transport=httpx.MockTransport(handler),
		)
		client = LiteLLMClient(
			self._settings(model="erp-structured"),
			async_client=async_client, langfuse_client=FakeAsyncLangfuseClient(),
		)
		try:
			result = await client.abuild_product_setup_draft(ChatRequest(
				messages=[ChatMessage(
					role="user",
					content="新增传承结晶1000个，售价9999元，批发价8800元，零售价10800元，成本价5000元",
				)],
				user="test@example.com", scenario="product_setup_draft",
			))
		finally:
			await async_client.aclose()

		self.assertEqual(captured["response_format"]["json_schema"]["name"], "product_setup_draft")
		self.assertFalse(captured["response_format"]["json_schema"]["schema"]["additionalProperties"])
		self.assertEqual(
			set(captured["response_format"]["json_schema"]["schema"]["required"]),
			set(captured["response_format"]["json_schema"]["schema"]["properties"]),
		)
		self.assertIn("product-setup-draft-v7", captured["messages"][0]["content"])
		self.assertEqual(result.draft.patch.item_name, "传承结晶")
		self.assertEqual(result.draft.patch.opening_qty, 1000)
		self.assertEqual(result.draft.patch.standard_selling_rate, 9999)
		self.assertEqual(result.draft.patch.wholesale_rate, 8800)
		self.assertEqual(result.draft.patch.retail_rate, 10800)
		self.assertEqual(result.draft.patch.standard_buying_rate, 5000)
