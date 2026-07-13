import json
from unittest import TestCase

import httpx

from myapp_ai.config import Settings
from myapp_ai.langfuse_client import LangfuseClient
from myapp_ai.schemas import ChatMessage, ChatRequest, TokenUsage


def _settings(**overrides) -> Settings:
	values = {
		"litellm_base_url": "http://litellm.test",
		"litellm_api_key": "test-key",
		"model": "erp-fast-chat",
		"reasoning_effort": "none",
		"service_token": "service-token",
		"timeout_seconds": 10,
		"max_messages": 20,
		"max_message_chars": 8000,
		"langfuse_host": "http://langfuse.test",
		"langfuse_public_key": "pk-test",
		"langfuse_secret_key": "sk-test",
		"langfuse_environment": "test",
		"langfuse_release": "release-1",
	}
	values.update(overrides)
	return Settings(**values)


class TestLangfuseClient(TestCase):
	def test_generation_ingestion_redacts_content_and_links_run(self):
		captured = {}

		def handler(request: httpx.Request):
			captured["authorization"] = request.headers.get("authorization")
			captured["body"] = json.loads(request.content)
			return httpx.Response(207, json={"successes": []})

		client = LangfuseClient(_settings(), transport=httpx.MockTransport(handler))
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="客户机密问题")],
			user="user@example.com",
			scenario="report_summary",
			company="rgc (Demo)",
			prompt_version="erp-readonly-v3",
			conversation_id="AI-CONV-1",
			run_id="AI-RUN-1",
		)

		synced = client.record_generation(
			request=request,
			trace_id="trace-1",
			generation_id="generation-1",
			started_at="2026-07-13T01:00:00+00:00",
			completed_at="2026-07-13T01:00:01+00:00",
			model="provider-model",
			model_alias="erp-fast-chat",
			output="包含敏感结果",
			usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
		)

		self.assertTrue(synced)
		serialized = json.dumps(captured["body"], ensure_ascii=False)
		self.assertNotIn("客户机密问题", serialized)
		self.assertNotIn("包含敏感结果", serialized)
		self.assertNotIn("user@example.com", serialized)
		self.assertIn("AI-RUN-1", serialized)
		self.assertIn("AI-CONV-1", serialized)
		self.assertIn("trace-1", serialized)
		self.assertTrue(captured["authorization"].startswith("Basic "))

	def test_feedback_ingestion_is_fail_open(self):
		def handler(_request: httpx.Request):
			return httpx.Response(503, json={"error": "unavailable"})

		client = LangfuseClient(_settings(), transport=httpx.MockTransport(handler))

		self.assertFalse(
			client.record_feedback(
				trace_id="trace-1",
				run_id="AI-RUN-1",
				rating="negative",
				category="incorrect",
				comment="结果不准确",
			)
		)

	def test_missing_configuration_disables_ingestion(self):
		client = LangfuseClient(
			_settings(langfuse_public_key="", langfuse_secret_key=""),
			transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
		)

		self.assertFalse(client.enabled)
		self.assertFalse(
			client.record_feedback(
				trace_id="trace-1",
				run_id="AI-RUN-1",
				rating="positive",
				category="helpful",
				comment=None,
			)
		)
