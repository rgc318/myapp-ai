import asyncio
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from myapp_ai.config import Settings, get_settings
from myapp_ai.main import _validated_prompt_request, app, health
from myapp_ai.policy import ResolvedPolicy
from myapp_ai.runtime_guard import RuntimeLimitExceeded
from myapp_ai.schemas import ChatMessage, ChatRequest


def _settings() -> Settings:
	return Settings(
		litellm_base_url="http://litellm.test", litellm_api_key="test-key",
		model="erp-fast-chat", reasoning_effort="none", service_token="service-token",
		timeout_seconds=10, max_messages=20, max_message_chars=8000,
	)


def _policy() -> ResolvedPolicy:
	return ResolvedPolicy(
		policy_code="general-prod", policy_version=1, model_alias="erp-fast-chat",
		reasoning_effort="none", max_completion_tokens=1000, timeout_seconds=30,
		max_concurrency=10, requests_per_minute=100, tokens_per_minute=10000,
		daily_budget=10, monthly_budget=100, budget_currency="CNY", budget_action="warn",
		fallback_model_aliases=(), model_costs={}, fallback_reason=None,
	)


class TestMain(TestCase):
	def setUp(self):
		app.dependency_overrides[get_settings] = _settings
		self.client_context = TestClient(app)
		self.client = self.client_context.__enter__()

	def tearDown(self):
		try:
			self.client_context.__exit__(None, None, None)
		finally:
			app.dependency_overrides.clear()

	def test_health_exposes_effective_prompt_versions(self):
		payload = health(_settings())

		self.assertEqual(payload["prompt_versions"]["general"], "erp-readonly-v5")
		self.assertEqual(payload["prompt_versions"]["sales_order_draft"], "sales-order-draft-v2")
		self.assertFalse(payload["vector_search_configured"])
		self.assertFalse(payload["runtime_governance_configured"])

	def test_runtime_rate_limit_returns_429_and_retry_after(self):
		policy = _policy()
		guard = patch("myapp_ai.main._runtime_guard").start()
		resolver = patch("myapp_ai.main._policy_resolver.resolve", return_value=policy).start()
		self.addCleanup(patch.stopall)
		guard.return_value.select_and_acquire.side_effect = RuntimeLimitExceeded(
			"AI_REQUEST_RATE_LIMITED", 23, "request rate limited",
		)

		response = self.client.post(
			"/internal/v1/chat",
			headers={"Authorization": "Bearer service-token"},
			json={"messages": [{"role": "user", "content": "你好"}], "user": "test@example.com"},
		)

		self.assertEqual(response.status_code, 429)
		self.assertEqual(response.headers["retry-after"], "23")
		self.assertEqual(response.json()["detail"]["code"], "AI_REQUEST_RATE_LIMITED")
		resolver.assert_called_once()

	def test_lifespan_reuses_shared_async_clients_across_requests(self):
		shared = app.state.http_clients
		status_payload = {
			"reachable": True, "collection_exists": False, "collection": "myapp-products-v1",
			"points_count": 0, "indexed_vectors_count": 0, "vector_size": None,
		}
		with patch("myapp_ai.main.ProductVectorClient") as client_class:
			client_class.return_value.astatus = AsyncMock(return_value=status_payload)
			for _index in range(2):
				response = self.client.post(
					"/internal/v1/vector/products/status",
					headers={"Authorization": "Bearer service-token"},
				)
				self.assertEqual(response.status_code, 200)

		self.assertIs(app.state.http_clients, shared)
		self.assertEqual(client_class.call_count, 2)
		for call in client_class.call_args_list:
			self.assertIs(call.kwargs["litellm_async_client"], shared.litellm)
			self.assertIs(call.kwargs["qdrant_async_client"], shared.qdrant)

	def test_full_local_chat_pool_returns_stable_429(self):
		shared = app.state.http_clients
		original = shared.chat_semaphore
		shared.chat_semaphore = asyncio.Semaphore(0)
		guard = Mock()
		guard.select_and_acquire.return_value = SimpleNamespace(
			model_alias="erp-fast-chat", fallback_reason=None,
		)
		try:
			with patch("myapp_ai.main._runtime_guard", return_value=guard), patch(
				"myapp_ai.main._policy_resolver.resolve", return_value=_policy(),
			):
				response = self.client.post(
					"/internal/v1/chat",
					headers={"Authorization": "Bearer service-token"},
					json={"messages": [{"role": "user", "content": "你好"}], "user": "test@example.com"},
				)
		finally:
			shared.chat_semaphore = original

		self.assertEqual(response.status_code, 429)
		self.assertEqual(response.headers["retry-after"], "1")
		self.assertEqual(response.json()["detail"]["code"], "AI_LOCAL_CONCURRENCY_LIMITED")
		guard.release.assert_called_once()

	def test_full_embedding_pool_returns_stable_429(self):
		shared = app.state.http_clients
		original = shared.embedding_semaphore
		shared.embedding_semaphore = asyncio.Semaphore(0)
		search = AsyncMock(return_value=[])
		try:
			with patch("myapp_ai.main.ProductVectorClient.asearch", new=search):
				response = self.client.post(
					"/internal/v1/vector/products/search",
					headers={"Authorization": "Bearer service-token"},
					json={"query": "蓝色包装饮料", "item_context": "sales", "limit": 8},
				)
		finally:
			shared.embedding_semaphore = original

		self.assertEqual(response.status_code, 429)
		self.assertEqual(response.headers["retry-after"], "1")
		self.assertEqual(response.json()["detail"]["code"], "AI_EMBEDDING_CONCURRENCY_LIMITED")
		search.assert_not_awaited()

	def test_prompt_version_mismatch_is_rejected_with_conflict(self):
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="你好")],
			user="test@example.com", scenario="general", prompt_version="erp-readonly-v4",
		)

		with self.assertRaises(HTTPException) as caught:
			_validated_prompt_request(request)

		self.assertEqual(caught.exception.status_code, 409)
		self.assertIn("expected erp-readonly-v5", caught.exception.detail)

	def test_blank_prompt_version_is_rejected_instead_of_silently_replaced(self):
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="你好")],
			user="test@example.com", scenario="general", prompt_version="",
		)

		with self.assertRaises(HTTPException) as caught:
			_validated_prompt_request(request)

		self.assertEqual(caught.exception.status_code, 409)

	def test_http_chat_and_draft_routes_return_prompt_conflict(self):
		headers = {"Authorization": "Bearer service-token"}
		endpoints = {
			"general": "/internal/v1/chat",
			"sales_order_draft": "/internal/v1/drafts/sales-order",
			"purchase_order_draft": "/internal/v1/drafts/purchase-order",
			"inventory_adjustment_draft": "/internal/v1/drafts/inventory-adjustment",
		}
		for scenario, endpoint in endpoints.items():
			with self.subTest(endpoint=endpoint):
				response = self.client.post(endpoint, headers=headers, json={
					"messages": [{"role": "user", "content": "测试"}],
					"user": "test@example.com",
					"scenario": scenario,
					"prompt_version": "stale-version",
				})
				self.assertEqual(response.status_code, 409)

	def test_feedback_endpoint_remains_accepted_when_observability_fails(self):
		with patch(
			"myapp_ai.main.LangfuseClient.arecord_feedback",
			new=AsyncMock(return_value=False),
		):
			response = self.client.post(
				"/internal/v1/feedback",
				headers={"Authorization": "Bearer service-token"},
				json={
					"trace_id": "trace-1", "run_id": "AI-RUN-1",
					"rating": "negative", "category": "incorrect",
					"comment": "结果不准确",
				},
			)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json(), {"accepted": True, "observability_synced": False})

	def test_vector_search_endpoint_requires_service_token_and_returns_matches(self):
		response = self.client.post(
			"/internal/v1/vector/products/search",
			json={"query": "蓝色包装饮料", "item_context": "sales", "limit": 8},
		)
		self.assertEqual(response.status_code, 401)

		with patch(
			"myapp_ai.main.ProductVectorClient.asearch",
			new=AsyncMock(return_value=[]),
		):
			response = self.client.post(
				"/internal/v1/vector/products/search",
				headers={"Authorization": "Bearer service-token"},
				json={"query": "蓝色包装饮料", "item_context": "sales", "limit": 8},
			)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()["matches"], [])

	def test_vector_status_endpoint_works_when_embedding_is_not_configured(self):
		with patch("myapp_ai.main.ProductVectorClient.astatus", new=AsyncMock(return_value={
			"reachable": True,
			"collection_exists": False,
			"collection": "myapp-products-v1",
			"points_count": 0,
			"indexed_vectors_count": 0,
			"vector_size": None,
		})):
			response = self.client.post(
				"/internal/v1/vector/products/status",
				headers={"Authorization": "Bearer service-token"},
			)

		self.assertEqual(response.status_code, 200)
		self.assertFalse(response.json()["vector_search_configured"])
		self.assertTrue(response.json()["reachable"])

	def test_governance_model_discovery_requires_service_token(self):
		response = self.client.get("/internal/v1/governance/models")
		self.assertEqual(response.status_code, 401)

		with patch("myapp_ai.main.discover_models", return_value=[{
			"model_alias": "erp-fast-chat", "capability": "fast_chat", "status": "active",
		}]):
			response = self.client.get(
				"/internal/v1/governance/models",
				headers={"Authorization": "Bearer service-token"},
			)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()["models"][0]["model_alias"], "erp-fast-chat")

	def test_governance_policy_validation_uses_internal_gate(self):
		with patch("myapp_ai.main.validate_policy", return_value={
			"release_gate_eligible": False,
			"errors": ["live gate missing"],
			"warnings": [],
			"evaluation": None,
		}):
			response = self.client.post(
				"/internal/v1/governance/validate-policy",
				headers={"Authorization": "Bearer service-token"},
				json={"policy": {
					"scenario": "general", "capability": "fast_chat",
					"primary_model_alias": "erp-fast-chat",
				}},
			)

		self.assertEqual(response.status_code, 200)
		self.assertFalse(response.json()["release_gate_eligible"])
