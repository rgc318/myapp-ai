import json
from unittest import IsolatedAsyncioTestCase, TestCase

import httpx

from myapp_ai.config import Settings
from myapp_ai.langfuse_client import LangfuseClient
from myapp_ai.schemas import ChatMessage, ChatRequest, TokenUsage


def _successful_ingestion_response(request: httpx.Request) -> httpx.Response:
	payload = json.loads(request.content)
	return httpx.Response(207, json={
		"successes": [{"id": event["id"], "status": 201} for event in payload["batch"]],
		"errors": [],
	})


def _otlp_span(payload: dict) -> dict:
	return payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]


def _attributes(rows: list[dict]) -> dict:
	result = {}
	for row in rows:
		value = row["value"]
		result[row["key"]] = next(iter(value.values()))
	return result


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
			captured["path"] = request.url.path
			captured["body"] = json.loads(request.content)
			return httpx.Response(200, json={"name": "otel-ingestion-job"})

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
			trace_id="1" * 32,
			generation_id="2" * 16,
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
		self.assertIn("1" * 32, serialized)
		self.assertTrue(captured["authorization"].startswith("Basic "))
		self.assertEqual(captured["path"], "/api/public/otel/v1/traces")
		resource = captured["body"]["resourceSpans"][0]["resource"]
		resource_attributes = _attributes(resource["attributes"])
		span = _otlp_span(captured["body"])
		span_attributes = _attributes(span["attributes"])
		self.assertEqual(resource_attributes["langfuse.environment"], "test")
		self.assertEqual(resource_attributes["langfuse.release"], "release-1")
		self.assertEqual(span["traceId"], "1" * 32)
		self.assertEqual(span["spanId"], "2" * 16)
		self.assertEqual(span_attributes["langfuse.observation.type"], "generation")
		self.assertEqual(span_attributes["langfuse.version"], "erp-readonly-v5")
		self.assertEqual(span_attributes["session.id"], "AI-CONV-1")
		self.assertIn("input_tokens", span_attributes["langfuse.observation.usage_details"])

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

	def test_feedback_comment_is_redacted_by_default(self):
		captured = {}

		def handler(request: httpx.Request):
			captured["body"] = json.loads(request.content)
			return _successful_ingestion_response(request)

		client = LangfuseClient(_settings(), transport=httpx.MockTransport(handler))
		self.assertTrue(
			client.record_feedback(
				trace_id="trace-1", run_id="AI-RUN-1", rating="negative",
				category="incorrect", comment="客户机密反馈说明",
			)
		)
		serialized = json.dumps(captured["body"], ensure_ascii=False)
		self.assertNotIn("客户机密反馈说明", serialized)
		body = captured["body"]["batch"][0]["body"]
		self.assertIsNone(body["comment"])
		self.assertEqual(body["environment"], "test")
		self.assertEqual(body["source"], "API")
		self.assertIn("sha256", body["metadata"]["comment_summary"])

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

	def test_partial_batch_errors_are_not_reported_as_success(self):
		def handler(_request: httpx.Request):
			return httpx.Response(207, json={
				"successes": [{"id": "one"}],
				"errors": [{"id": "two", "message": "invalid"}],
			})

		client = LangfuseClient(_settings(), transport=httpx.MockTransport(handler))
		self.assertFalse(
			client.record_feedback(
				trace_id="trace-1", run_id="AI-RUN-1", rating="positive",
				category="helpful", comment=None,
			)
		)

	def test_missing_batch_successes_are_not_reported_as_success(self):
		client = LangfuseClient(
			_settings(),
			transport=httpx.MockTransport(
				lambda _request: httpx.Response(207, json={"successes": [], "errors": []})
			),
		)

		self.assertFalse(
			client.record_feedback(
				trace_id="trace-1", run_id="AI-RUN-1", rating="positive",
				category="helpful", comment=None,
			)
		)

	def test_otlp_generation_http_error_is_fail_open(self):
		def handler(_request: httpx.Request):
			return httpx.Response(503, json={"error": "unavailable"})

		client = LangfuseClient(_settings(), transport=httpx.MockTransport(handler))
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="测试多事件批次")],
			user="user@example.com", scenario="general",
		)
		self.assertFalse(
			client.record_generation(
				request=request, trace_id="trace-partial", generation_id="generation-partial",
				started_at="2026-07-13T01:00:00+00:00",
				completed_at="2026-07-13T01:00:01+00:00",
				model="provider-model", model_alias="erp-fast-chat", output="测试输出",
				usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
			)
		)

	def test_draft_generation_uses_registry_prompt_version_in_metadata(self):
		captured = {}

		def handler(request: httpx.Request):
			captured["body"] = json.loads(request.content)
			return httpx.Response(200, json={"name": "otel-ingestion-job"})

		client = LangfuseClient(_settings(), transport=httpx.MockTransport(handler))
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="采购两箱相机")],
			user="user@example.com", scenario="purchase_order_draft",
			prompt_version="erp-readonly-v3",
		)
		self.assertTrue(
			client.record_generation(
				request=request, trace_id="trace-draft", generation_id="generation-draft",
				started_at="2026-07-13T01:00:00+00:00",
				completed_at="2026-07-13T01:00:01+00:00",
				model="provider-model", model_alias="erp-structured",
				output="{}", usage=TokenUsage(),
			)
		)
		serialized = json.dumps(captured["body"], ensure_ascii=False)
		self.assertIn("purchase-order-draft-v2", serialized)
		self.assertNotIn('"prompt_version": "erp-readonly-v3"', serialized)

	def test_evaluation_scores_link_to_trace_without_raw_content(self):
		captured = {}

		def handler(request: httpx.Request):
			captured["body"] = json.loads(request.content)
			return _successful_ingestion_response(request)

		client = LangfuseClient(_settings(), transport=httpx.MockTransport(handler))
		self.assertTrue(
			client.record_evaluation_scores(
				trace_id="trace-eval", case_id="chat.write_action_refusal",
				dataset_version="ai-core-v1", prompt_version="erp-readonly-v3",
				mode="live", attempt=1,
				scores={"case_pass": 1.0, "safety_pass": 1.0},
			)
		)
		batch = captured["body"]["batch"]
		self.assertEqual({event["body"]["name"] for event in batch}, {"eval.case_pass", "eval.safety_pass"})
		self.assertTrue(all(event["body"]["traceId"] == "trace-eval" for event in batch))
		self.assertTrue(all(event["body"]["environment"] == "test" for event in batch))
		self.assertTrue(all(event["body"]["source"] == "EVAL" for event in batch))
		serialized = json.dumps(batch, ensure_ascii=False)
		self.assertNotIn("直接替我创建", serialized)


class TestAsyncLangfuseClient(IsolatedAsyncioTestCase):
	async def test_async_generation_uses_otlp_and_remains_fail_open(self):
		paths = []

		def handler(request: httpx.Request):
			paths.append(request.url.path)
			return httpx.Response(503, json={"error": "unavailable"})

		async_client = httpx.AsyncClient(
			base_url="http://langfuse.test", transport=httpx.MockTransport(handler),
		)
		try:
			synced = await LangfuseClient(
				_settings(), async_client=async_client,
			).arecord_generation(
				request=ChatRequest(
					messages=[ChatMessage(role="user", content="测试")],
					user="user@example.com", scenario="general",
				),
				trace_id="1" * 32, generation_id="2" * 16,
				started_at="2026-07-13T01:00:00+00:00",
				completed_at="2026-07-13T01:00:01+00:00",
				model="provider-model", model_alias="erp-fast-chat",
				output="测试输出", usage=TokenUsage(),
			)
		finally:
			await async_client.aclose()

		self.assertFalse(synced)
		self.assertEqual(paths, ["/api/public/otel/v1/traces"])

	async def test_async_feedback_ingestion_is_fail_open(self):
		async_client = httpx.AsyncClient(
			base_url="http://langfuse.test",
			transport=httpx.MockTransport(
				lambda _request: httpx.Response(503, json={"error": "unavailable"})
			),
		)
		try:
			synced = await LangfuseClient(
				_settings(), async_client=async_client,
			).arecord_feedback(
				trace_id="trace-1", run_id="AI-RUN-1", rating="negative",
				category="incorrect", comment="结果不准确",
			)
		finally:
			await async_client.aclose()

		self.assertFalse(synced)
