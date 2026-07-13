from unittest import TestCase
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from myapp_ai.config import Settings, get_settings
from myapp_ai.main import _validated_prompt_request, app, health
from myapp_ai.schemas import ChatMessage, ChatRequest


def _settings() -> Settings:
	return Settings(
		litellm_base_url="http://litellm.test", litellm_api_key="test-key",
		model="erp-fast-chat", reasoning_effort="none", service_token="service-token",
		timeout_seconds=10, max_messages=20, max_message_chars=8000,
	)


class TestMain(TestCase):
	def setUp(self):
		app.dependency_overrides[get_settings] = _settings
		self.client = TestClient(app)

	def tearDown(self):
		app.dependency_overrides.clear()

	def test_health_exposes_effective_prompt_versions(self):
		payload = health(_settings())

		self.assertEqual(payload["prompt_versions"]["general"], "erp-readonly-v5")
		self.assertEqual(payload["prompt_versions"]["sales_order_draft"], "sales-order-draft-v2")

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
		with patch("myapp_ai.main.LangfuseClient.record_feedback", return_value=False):
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
