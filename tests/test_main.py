import asyncio
import hashlib
from dataclasses import replace
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from myapp_ai.config import Settings, get_settings
from myapp_ai.main import (
	ModelProviderRejected,
	_execute_governed,
	_resolve_governed_policy,
	_validated_prompt_request,
	_with_requested_model,
	_with_required_modalities,
	_with_scenario_eligible_models,
	app,
	health,
	stream_chat,
)
from myapp_ai.policy import ResolvedPolicy
from myapp_ai.runtime_guard import RuntimeLimitExceeded
from myapp_ai.schemas import ChatMessage, ChatRequest, ChatResponse, ImageAttachment, TokenUsage


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
		payload = health(SimpleNamespace(app=app), _settings())

		self.assertEqual(payload["protocol_version"], "ai-runtime-contract-v1")
		self.assertEqual(payload["prompt_versions"]["general"], "erp-readonly-v11")
		self.assertEqual(payload["prompt_versions"]["intent_parse"], "erp-intent-v7")
		self.assertEqual(payload["prompt_versions"]["sales_order_draft"], "sales-order-draft-v5")
		self.assertEqual(payload["prompt_versions"]["product_setup_draft"], "product-setup-draft-v7")
		self.assertEqual(payload["runtime_revision"], "unversioned")
		self.assertEqual(payload["release_id"], "unversioned")
		self.assertEqual(len(payload["prompt_manifest_sha256"]), 64)
		self.assertEqual(len(payload["schema_manifest_sha256"]), 64)
		self.assertEqual(len(payload["tool_manifest_sha256"]), 64)
		self.assertFalse(payload["vector_search_configured"])
		self.assertFalse(payload["runtime_governance_configured"])
		self.assertIn("langfuse_delivery", payload)

	def test_liveness_and_readiness_separate_process_and_runtime_status(self):
		live_response = self.client.get("/livez")
		ready_response = self.client.get("/readyz")

		self.assertEqual(live_response.status_code, 200)
		self.assertEqual(live_response.json(), {"status": "alive"})
		self.assertEqual(ready_response.status_code, 200)
		payload = ready_response.json()
		self.assertTrue(payload["ready"])
		self.assertEqual(payload["status"], "degraded")
		self.assertEqual(payload["protocol_version"], "ai-runtime-contract-v1")
		self.assertEqual(payload["checks"]["litellm"]["status"], "ready")
		self.assertEqual(payload["checks"]["runtime_governance"]["status"], "degraded")
		self.assertEqual(
			payload["scenarios"]["product_setup_draft"]["prompt_version"],
			"product-setup-draft-v7",
		)
		self.assertEqual(
			payload["scenarios"]["product_setup_draft"]["schema_families"],
			{"product_setup_draft": ["product-setup-draft-v1"]},
		)
		self.assertEqual(payload["supported_protocol_range"], ["ai-runtime-contract-v1"])
		self.assertIn("runtime-response-metadata-v1", payload["capabilities"])

	def test_readiness_blocks_when_litellm_is_not_configured(self):
		app.dependency_overrides[get_settings] = lambda: replace(_settings(), litellm_api_key="")

		response = self.client.get("/readyz")

		self.assertEqual(response.status_code, 503)
		self.assertFalse(response.json()["ready"])
		self.assertEqual(response.json()["status"], "blocked")
		self.assertEqual(response.json()["checks"]["litellm"]["status"], "blocked")

	def test_prompt_version_mismatch_returns_structured_contract_error(self):
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="新增商品")],
			user="test@example.com",
			scenario="product_setup_draft",
			prompt_version="product-setup-draft-v6",
		)

		with self.assertRaises(HTTPException) as raised:
			_validated_prompt_request(request, scenario="product_setup_draft")

		self.assertEqual(raised.exception.status_code, 409)
		self.assertEqual(raised.exception.detail, {
			"code": "AI_PROMPT_VERSION_MISMATCH",
			"category": "contract",
			"layer": "orchestrator",
			"retryable": False,
			"message": "AI prompt contract version does not match the running orchestrator.",
			"scenario": "product_setup_draft",
			"received_version": "product-setup-draft-v6",
			"expected_version": "product-setup-draft-v7",
		})

	def test_new_runtime_contract_negotiates_schema_and_ignores_stale_prompt_revision(self):
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="新增商品")],
			user="test@example.com",
			scenario="product_setup_draft",
			protocol_version="ai-runtime-contract-v1",
			supported_schema_versions=["product-setup-draft-v1"],
			prompt_version="product-setup-draft-v6",
		)

		validated = _validated_prompt_request(
			request, scenario="product_setup_draft", schema_family="product_setup_draft",
		)

		self.assertEqual(validated.schema_version, "product-setup-draft-v1")
		self.assertEqual(validated.prompt_version, "product-setup-draft-v7")

	def test_new_runtime_contract_rejects_unsupported_protocol(self):
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="你好")],
			user="test@example.com",
			protocol_version="ai-runtime-contract-v0",
			supported_schema_versions=["chat-v1"],
		)

		with self.assertRaises(HTTPException) as raised:
			_validated_prompt_request(request, schema_family="chat")

		self.assertEqual(raised.exception.status_code, 409)
		self.assertEqual(raised.exception.detail["code"], "AI_RUNTIME_CONTRACT_MISMATCH")
		self.assertEqual(raised.exception.detail["supported"], ["ai-runtime-contract-v1"])

	def test_new_runtime_contract_rejects_unsupported_schema(self):
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="你好")],
			user="test@example.com",
			protocol_version="ai-runtime-contract-v1",
			supported_schema_versions=["chat-v0"],
		)

		with self.assertRaises(HTTPException) as raised:
			_validated_prompt_request(request, schema_family="chat")

		self.assertEqual(raised.exception.status_code, 409)
		self.assertEqual(raised.exception.detail["code"], "AI_SCHEMA_VERSION_MISMATCH")
		self.assertEqual(raised.exception.detail["supported"], ["chat-v1"])

	def test_governed_response_carries_selected_runtime_contract_and_prompt_revision(self):
		request = _validated_prompt_request(ChatRequest(
			messages=[ChatMessage(role="user", content="你好")],
			user="test@example.com",
			protocol_version="ai-runtime-contract-v1",
			supported_schema_versions=["chat-v1"],
		), schema_family="chat")
		guard = Mock()
		guard.select_and_acquire.return_value = SimpleNamespace(
			model_alias="erp-fast-chat", fallback_reason=None,
		)
		response = ChatResponse(
			message=ChatMessage(role="assistant", content="你好"),
			model="provider-model",
			model_alias="erp-fast-chat",
			trace_id="trace-1",
			usage=TokenUsage(total_tokens=2),
		)
		clients = SimpleNamespace(chat_semaphore=asyncio.Semaphore(1))

		async def execute():
			with patch("myapp_ai.main._policy_resolver.resolve", return_value=_policy()), patch(
				"myapp_ai.main._runtime_guard", return_value=guard,
			), patch(
				"myapp_ai.main._client_for_policy", return_value=Mock(),
			):
				return await _execute_governed(
					_settings(), request, AsyncMock(return_value=response),
					clients=clients, semaphore=clients.chat_semaphore,
				)

		result = asyncio.run(execute())

		self.assertEqual(result.protocol_version, "ai-runtime-contract-v1")
		self.assertEqual(result.schema_version, "chat-v1")
		self.assertEqual(result.prompt_version, "erp-readonly-v11")
		self.assertEqual(result.runtime_revision, "unversioned")
		self.assertEqual(result.release_id, "unversioned")

	def test_message_level_image_requires_a_validated_vision_model(self):
		request = ChatRequest(
			messages=[ChatMessage(
				role="user", content="继续分析图片", attachments=[ImageAttachment(
					attachment_id="AI-ATT-1", mime_type="image/webp",
					sha256=hashlib.sha256(b"x").hexdigest(), data_base64="eA==",
				)],
			)],
			user="user@example.com",
		)

		with self.assertRaises(HTTPException) as raised:
			_with_required_modalities(_policy(), request)

		self.assertEqual(raised.exception.detail["code"], "AI_VISION_MODEL_REQUIRED")

	def test_explicit_model_selection_disables_silent_fallbacks(self):
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="你好")],
			user="test@example.com",
			model_alias="opencode-glm-5.2",
		)
		selected = _with_requested_model(
			replace(_policy(), fallback_model_aliases=("fallback-model",)),
			request,
		)

		self.assertEqual(selected.model_alias, "opencode-glm-5.2")
		self.assertEqual(selected.fallback_model_aliases, ())

	def test_stream_auto_model_falls_back_before_first_visible_delta(self):
		class FailingClient:
			async def astream(self, _request):
				raise httpx.ConnectError("primary unavailable")
				yield  # pragma: no cover

		class HealthyClient:
			async def astream(self, _request):
				yield {"type": "started", "model_alias": "fallback-model"}
				yield {"type": "message_delta", "delta": "已切换"}
				yield {
					"type": "completed",
					"usage": {
						"prompt_tokens": 2,
						"completion_tokens": 2,
						"total_tokens": 4,
					},
				}

		policy = replace(
			_policy(),
			fallback_model_aliases=("fallback-model",),
			model_costs={"erp-fast-chat": {}, "fallback-model": {}},
		)
		guard = Mock()
		guard.select_and_acquire.return_value = SimpleNamespace(
			model_alias="erp-fast-chat", fallback_reason=None,
		)
		guard.acquire_fallback_after_failure.return_value = SimpleNamespace(
			model_alias="fallback-model", fallback_reason="provider_error_fallback",
		)
		clients = SimpleNamespace(chat_semaphore=asyncio.Semaphore(1))
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="你好")],
			user="test@example.com",
		)

		async def consume():
			with patch(
				"myapp_ai.main._policy_resolver.resolve", return_value=policy,
			), patch("myapp_ai.main._runtime_guard", return_value=guard), patch(
				"myapp_ai.main._client_for_policy",
				side_effect=lambda _settings, selected, _clients: (
					FailingClient()
					if selected.model_alias == "erp-fast-chat"
					else HealthyClient()
				),
			):
				response = await stream_chat(request, _settings(), clients)
				return "".join([chunk async for chunk in response.body_iterator])

		body = asyncio.run(consume())

		self.assertIn('"model_alias":"fallback-model"', body)
		self.assertIn('"delta":"已切换"', body)
		self.assertIn('"fallback_reason":"provider_error_fallback"', body)
		guard.acquire_fallback_after_failure.assert_called_once()

	def test_stream_fixed_model_does_not_silently_fallback(self):
		class FailingClient:
			async def astream(self, _request):
				raise httpx.ConnectError("fixed model unavailable")
				yield  # pragma: no cover

		policy = replace(
			_policy(),
			fallback_model_aliases=("fallback-model",),
			model_costs={"fixed-model": {}, "fallback-model": {}},
		)
		guard = Mock()
		guard.select_and_acquire.return_value = SimpleNamespace(
			model_alias="fixed-model", fallback_reason=None,
		)
		clients = SimpleNamespace(chat_semaphore=asyncio.Semaphore(1))
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="你好")],
			user="test@example.com",
			model_alias="fixed-model",
		)

		async def consume():
			with patch(
				"myapp_ai.main._policy_resolver.resolve", return_value=policy,
			), patch("myapp_ai.main._runtime_guard", return_value=guard), patch(
				"myapp_ai.main._client_for_policy", return_value=FailingClient(),
			):
				response = await stream_chat(request, _settings(), clients)
				return "".join([chunk async for chunk in response.body_iterator])

		body = asyncio.run(consume())

		self.assertIn('"type":"error"', body)
		self.assertIn('"model_alias":"fixed-model"', body)
		guard.acquire_fallback_after_failure.assert_not_called()

	def test_agent_policy_resolution_reuses_matching_cached_snapshot(self):
		policy = replace(_policy(), model_costs={
			"erp-fast-chat": {"status": "active", "supports_tools": True},
		})
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="查询商品")],
			user="test@example.com", company="Demo Company",
			policy_code=policy.policy_code, policy_version=policy.policy_version,
		)
		with patch("myapp_ai.main._policy_resolver.resolve", return_value=policy) as resolver:
			resolved = asyncio.run(
				_resolve_governed_policy(_settings(), request, require_tools=True)
			)

		self.assertEqual(resolved, policy)
		resolver.assert_called_once_with(_settings(), request)

	def test_fixed_model_health_unavailable_refreshes_cached_snapshot(self):
		stale = replace(_policy(), model_costs={
			"fixed-model": {"last_health_status": "unavailable"},
		})
		current = replace(_policy(), model_costs={
			"fixed-model": {"last_health_status": "available"},
		})
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="你好")],
			user="test@example.com", model_alias="fixed-model",
		)
		with patch(
			"myapp_ai.main._policy_resolver.resolve", side_effect=[stale, current],
		) as resolver:
			resolved = asyncio.run(_resolve_governed_policy(_settings(), request))

		self.assertEqual(resolved, current)
		self.assertEqual(resolver.call_count, 2)
		self.assertEqual(resolver.call_args_list[1].kwargs, {"force_refresh": True})

	def test_fixed_model_half_open_health_does_not_refresh_cached_snapshot(self):
		policy = replace(_policy(), model_costs={
			"fixed-model": {
				"last_health_status": "unavailable",
				"effective_health_status": "half_open",
			},
		})
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="你好")],
			user="test@example.com", model_alias="fixed-model",
		)
		with patch(
			"myapp_ai.main._policy_resolver.resolve", return_value=policy,
		) as resolver:
			resolved = asyncio.run(_resolve_governed_policy(_settings(), request))

		self.assertEqual(resolved, policy)
		resolver.assert_called_once_with(_settings(), request)

	def test_agent_model_eligibility_promotes_validated_fallback(self):
		policy = replace(
			_policy(),
			fallback_model_aliases=("tool-fallback",),
			model_costs={
				"erp-fast-chat": {"status": "active", "supports_tools": False},
				"tool-fallback": {"status": "validated", "supports_tools": True},
			},
		)
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="查询商品")],
			user="test@example.com", company="Demo Company",
		)

		resolved = _with_scenario_eligible_models(policy, request, require_tools=True)

		self.assertEqual(resolved.model_alias, "tool-fallback")
		self.assertEqual(resolved.fallback_model_aliases, ())
		self.assertEqual(resolved.fallback_reason, "primary_model_ineligible_for_scenario")

	def test_fixed_model_ineligible_for_agent_returns_stable_error(self):
		policy = replace(_policy(), model_costs={
			"fixed-model": {"status": "active", "supports_tools": False},
		})
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="查询商品")],
			user="test@example.com", company="Demo Company", model_alias="fixed-model",
		)
		policy = _with_requested_model(policy, request)

		with self.assertRaises(HTTPException) as raised:
			_with_scenario_eligible_models(policy, request, require_tools=True)

		self.assertEqual(raised.exception.status_code, 422)
		self.assertEqual(raised.exception.detail["code"], "AI_SELECTED_MODEL_INELIGIBLE")
		self.assertEqual(raised.exception.detail["reasons"], ["tools_unverified"])

	def test_agent_policy_without_eligible_models_returns_stable_error(self):
		policy = replace(
			_policy(),
			fallback_model_aliases=("disabled-fallback",),
			model_costs={
				"erp-fast-chat": {"status": "active", "supports_tools": False},
				"disabled-fallback": {"status": "disabled", "supports_tools": True},
			},
		)
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="查询商品")],
			user="test@example.com", company="Demo Company",
		)

		with self.assertRaises(HTTPException) as raised:
			_with_scenario_eligible_models(policy, request, require_tools=True)

		self.assertEqual(raised.exception.status_code, 503)
		self.assertEqual(raised.exception.detail["code"], "AI_SCENARIO_MODEL_UNAVAILABLE")

	def test_structured_scenario_promotes_model_with_validated_structured_output(self):
		policy = replace(_policy(), model_costs={
			"erp-fast-chat": {
				"status": "active", "supports_structured_output": False,
			},
			"structured-fallback": {
				"status": "validated", "supports_structured_output": True,
			},
		}, fallback_model_aliases=("structured-fallback",))
		request = ChatRequest(
			user="user@example.com", scenario="sales_order_draft",
			messages=[{"role": "user", "content": "create an order"}],
		)

		resolved = _with_scenario_eligible_models(policy, request, require_tools=False)

		self.assertEqual(resolved.model_alias, "structured-fallback")
		self.assertEqual(resolved.fallback_reason, "primary_model_ineligible_for_scenario")

	def test_fixed_structured_scenario_rejects_unvalidated_structured_output(self):
		policy = replace(_policy(), model_alias="fixed-model", model_costs={
			"fixed-model": {"status": "active", "supports_structured_output": False},
		})
		request = ChatRequest(
			user="user@example.com", scenario="intent_parse", model_alias="fixed-model",
			messages=[{"role": "user", "content": "hello"}],
		)

		with self.assertRaises(HTTPException) as raised:
			_with_scenario_eligible_models(policy, request, require_tools=False)

		self.assertEqual(raised.exception.detail["code"], "AI_SELECTED_MODEL_INELIGIBLE")
		self.assertEqual(
			raised.exception.detail["reasons"], ["structured_output_unverified"],
		)

	def test_auto_model_refreshes_when_every_candidate_is_cached_unavailable(self):
		stale = replace(
			_policy(),
			fallback_model_aliases=("fallback-model",),
			model_costs={
				"erp-fast-chat": {"last_health_status": "unavailable"},
				"fallback-model": {"last_health_status": "unavailable"},
			},
		)
		current = replace(stale, model_costs={
			"erp-fast-chat": {"last_health_status": "available"},
			"fallback-model": {"last_health_status": "unavailable"},
		})
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="你好")],
			user="test@example.com",
		)
		with patch(
			"myapp_ai.main._policy_resolver.resolve", side_effect=[stale, current],
		) as resolver:
			resolved = asyncio.run(_resolve_governed_policy(_settings(), request))

		self.assertEqual(resolved, current)
		self.assertEqual(resolver.call_count, 2)
		self.assertEqual(resolver.call_args_list[1].kwargs, {"force_refresh": True})

	def test_new_agent_policy_resolution_requires_version_handshake(self):
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="查询商品")],
			user="test@example.com", company="Demo Company",
		)
		with patch("myapp_ai.main._policy_resolver.resolve") as resolver:
			with self.assertRaises(HTTPException) as raised:
				asyncio.run(_resolve_governed_policy(
					_settings(), request, require_tools=True, require_policy_handshake=True,
				))

		self.assertEqual(raised.exception.status_code, 422)
		self.assertEqual(
			raised.exception.detail["code"], "AI_AGENT_POLICY_HANDSHAKE_REQUIRED",
		)
		resolver.assert_not_called()

	def test_agent_policy_resolution_refreshes_stale_cached_snapshot(self):
		stale = replace(_policy(), policy_version=1, model_costs={})
		current = replace(_policy(), policy_version=2, model_costs={
			"erp-fast-chat": {"status": "active", "supports_tools": True},
		})
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="查询商品")],
			user="test@example.com", company="Demo Company",
			policy_code="general-prod", policy_version=2,
		)
		with patch(
			"myapp_ai.main._policy_resolver.resolve", side_effect=[stale, current],
		) as resolver:
			resolved = asyncio.run(
				_resolve_governed_policy(_settings(), request, require_tools=True)
			)

		self.assertEqual(resolved, current)
		self.assertEqual(resolver.call_count, 2)
		self.assertEqual(resolver.call_args_list[1].kwargs, {"force_refresh": True})

	def test_agent_policy_resolution_rejects_snapshot_version_mismatch(self):
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="查询商品")],
			user="test@example.com", company="Demo Company",
			policy_code="general-prod", policy_version=2,
		)
		with patch("myapp_ai.main._policy_resolver.resolve", return_value=_policy()) as resolver:
			with self.assertRaises(HTTPException) as raised:
				asyncio.run(_resolve_governed_policy(_settings(), request, require_tools=True))

		self.assertEqual(raised.exception.status_code, 503)
		self.assertEqual(
			raised.exception.detail["code"], "AI_AGENT_POLICY_SNAPSHOT_MISMATCH",
		)
		self.assertEqual(resolver.call_count, 2)

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

	def test_policy_cache_invalidation_requires_service_token_and_invalidates_resolver(self):
		without_token = self.client.post(
			"/internal/v1/governance/policy-cache/invalidate",
		)
		self.assertEqual(without_token.status_code, 401)

		with patch("myapp_ai.main._policy_resolver.invalidate") as invalidate:
			response = self.client.post(
				"/internal/v1/governance/policy-cache/invalidate",
				headers={"Authorization": "Bearer service-token"},
			)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json(), {"invalidated": True})
		invalidate.assert_called_once_with()

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
		self.assertEqual(caught.exception.detail["code"], "AI_PROMPT_VERSION_MISMATCH")
		self.assertEqual(caught.exception.detail["scenario"], "general")
		self.assertEqual(caught.exception.detail["received_version"], "erp-readonly-v4")
		self.assertEqual(caught.exception.detail["expected_version"], "erp-readonly-v11")

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
			"intent_parse": "/internal/v1/intent/parse",
			"sales_order_draft": "/internal/v1/drafts/sales-order",
			"purchase_order_draft": "/internal/v1/drafts/purchase-order",
			"inventory_adjustment_draft": "/internal/v1/drafts/inventory-adjustment",
			"product_setup_draft": "/internal/v1/drafts/product-setup",
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

	def test_structured_draft_provider_error_identifies_selected_model(self):
		with patch(
			"myapp_ai.main._execute_governed",
			new=AsyncMock(side_effect=ModelProviderRejected(
				model_alias="opencode-deepseek-v4-flash",
				provider_status=403,
			)),
		):
			response = self.client.post(
				"/internal/v1/drafts/product-setup",
				headers={"Authorization": "Bearer service-token"},
				json={
					"messages": [{"role": "user", "content": "完善迪莫商品资料"}],
					"user": "test@example.com",
					"scenario": "product_setup_draft",
					"prompt_version": "product-setup-draft-v7",
				},
			)

		self.assertEqual(response.status_code, 502)
		self.assertEqual(response.json()["detail"], {
			"code": "MODEL_PROVIDER_REJECTED",
			"message": "模型供应商拒绝了请求。",
			"model_alias": "opencode-deepseek-v4-flash",
			"provider_error_code": "PROVIDER_HTTP_403",
		})

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
		self.assertEqual(response.json()["source"], "litellm")
		self.assertEqual(response.json()["visible_count"], 1)

	def test_governance_model_availability_requires_service_token(self):
		response = self.client.post("/internal/v1/governance/models/availability", json={})
		self.assertEqual(response.status_code, 401)

		with patch("myapp_ai.main.check_model_availability", return_value={
			"source": "litellm",
			"checked_count": 1,
			"available_count": 1,
			"unavailable_count": 0,
			"items": [{"model_alias": "erp-fast-chat", "available": True}],
		}):
			response = self.client.post(
				"/internal/v1/governance/models/availability",
				headers={"Authorization": "Bearer service-token"},
				json={"model_aliases": ["erp-fast-chat"]},
			)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()["available_count"], 1)

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
