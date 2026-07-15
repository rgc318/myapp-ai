import json
from unittest import IsolatedAsyncioTestCase, TestCase

import httpx

from myapp_ai.config import Settings
from myapp_ai.litellm_client import LiteLLMClient
from myapp_ai.schemas import ChatMessage, ChatRequest


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
		self.assertIn("Prompt 版本：inventory-adjustment-draft-v2", captured["messages"][0]["content"])
		self.assertEqual(result.draft.adjustment_type, "set_target")
		self.assertEqual(result.draft.quantity, 8)

	def test_build_purchase_order_draft_uses_purchase_schema(self):
		captured = {}

		def handler(request: httpx.Request):
			captured.update(json.loads(request.content))
			return httpx.Response(200, json={
				"model": "structured-model",
				"choices": [{"message": {"content": json.dumps({
					"supplier_query": "供应商A", "transaction_date": None, "schedule_date": None,
					"default_purchase_mode": "wholesale", "warehouse_query": None,
					"currency": None, "supplier_ref": None, "remarks": None,
					"items": [{"item_query": "相机", "qty": 2, "uom": "Box", "price": None, "warehouse_query": None}],
				}, ensure_ascii=False)}}],
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
		self.assertEqual(result.draft.supplier_query, "供应商A")
		self.assertEqual(result.draft.items[0].qty, 2)

	def test_build_sales_order_draft_uses_strict_json_schema(self):
		captured = {}

		def handler(request: httpx.Request):
			captured.update(json.loads(request.content))
			return httpx.Response(
				200,
				json={
					"model": "structured-model",
					"choices": [{"message": {"content": json.dumps({
						"customer_query": "客户A", "transaction_date": None,
						"delivery_date": None, "default_sales_mode": "wholesale",
						"warehouse_query": None, "remarks": None,
						"items": [{"item_query": "相机", "qty": 2, "uom": "Box", "price": None, "warehouse_query": None}],
					}, ensure_ascii=False)}}],
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
		self.assertIn("Prompt 版本：sales-order-draft-v2", captured["messages"][0]["content"])
		self.assertEqual(result.draft.customer_query, "客户A")
		self.assertEqual(result.draft.items[0].qty, 2)

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
			prompt_version="erp-readonly-v5",
		)

		langfuse = FakeLangfuseClient()
		result = LiteLLMClient(
			settings,
			transport=httpx.MockTransport(handler),
			langfuse_client=langfuse,
		).chat(request)

		self.assertEqual(captured["model"], "gpt-5.5")
		self.assertEqual(captured["reasoning_effort"], "none")
		self.assertRegex(captured["user"], r"^myapp-[0-9a-f]{64}$")
		self.assertNotIn("test@example.com", json.dumps(captured, ensure_ascii=False))
		self.assertIn("ITEM-001", captured["messages"][0]["content"])
		self.assertIn("erp-readonly-v5", captured["messages"][0]["content"])
		self.assertEqual(result.message.content, "你好")
		self.assertEqual(result.usage.reasoning_tokens, 0)
		self.assertEqual(len(result.warnings), 1)
		self.assertEqual(len(langfuse.generations), 1)
		self.assertEqual(langfuse.generations[0]["output"], "你好")
		self.assertEqual(langfuse.generations[0]["request"].prompt_version, "erp-readonly-v5")

	def test_sales_draft_falls_back_from_rejected_json_schema_and_keeps_prompt_version(self):
		captured = []

		def handler(request: httpx.Request):
			payload = json.loads(request.content)
			captured.append(payload)
			if len(captured) == 1:
				return httpx.Response(400, json={"error": "response_format unsupported"})
			return httpx.Response(200, json={
				"model": "fallback-model",
				"choices": [{"message": {"content": json.dumps({
					"customer_query": "客户A", "transaction_date": None,
					"delivery_date": None, "default_sales_mode": "wholesale",
					"warehouse_query": None, "remarks": None,
					"items": [{"item_query": "相机", "qty": 2, "uom": "Box", "price": None, "warehouse_query": None}],
				}, ensure_ascii=False)}}],
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
		self.assertIn("sales-order-draft-v2", captured[1]["messages"][0]["content"])
		self.assertEqual(result.draft.customer_query, "客户A")
		self.assertEqual(langfuse.generations[0]["request"].scenario, "sales_order_draft")
		self.assertEqual(langfuse.generations[0]["request"].prompt_version, "sales-order-draft-v2")

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
				"choices": [{"message": {"content": json.dumps({
					"customer_query": "客户A", "transaction_date": None,
					"delivery_date": None, "default_sales_mode": "wholesale",
					"warehouse_query": None, "remarks": None,
					"items": [{"item_query": "相机", "qty": 2, "uom": "Box", "price": None, "warehouse_query": None}],
				}, ensure_ascii=False)}}],
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

		self.assertEqual(result.draft.customer_query, "客户A")
		self.assertEqual(len(payloads), 2)
		self.assertIn("response_format", payloads[0])
		self.assertNotIn("response_format", payloads[1])
		self.assertIn("sales-order-draft-v2", payloads[1]["messages"][0]["content"])
		self.assertEqual(langfuse.generations[0]["request"].scenario, "sales_order_draft")
