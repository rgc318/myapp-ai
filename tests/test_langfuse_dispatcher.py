import asyncio
import json
from unittest import IsolatedAsyncioTestCase

import httpx

from myapp_ai.config import Settings
from myapp_ai.langfuse_dispatcher import LangfuseGenerationDispatcher
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
		"langfuse_queue_capacity": 10,
		"langfuse_batch_size": 10,
		"langfuse_flush_interval_seconds": 0.02,
		"langfuse_max_retries": 2,
		"langfuse_shutdown_timeout_seconds": 1,
	}
	values.update(overrides)
	return Settings(**values)


def _generation(index: int) -> dict:
	return {
		"request": ChatRequest(
			messages=[ChatMessage(role="user", content=f"问题 {index}")],
			user="user@example.com",
			run_id=f"AI-RUN-{index}",
		),
		"trace_id": f"{index + 1:032x}",
		"generation_id": f"{index + 1:016x}",
		"started_at": "2026-07-15T00:00:00+00:00",
		"completed_at": "2026-07-15T00:00:01+00:00",
		"model": "provider-model",
		"model_alias": "erp-fast-chat",
		"output": f"回答 {index}",
		"usage": TokenUsage(prompt_tokens=2, completion_tokens=3, total_tokens=5),
	}


class TestLangfuseGenerationDispatcher(IsolatedAsyncioTestCase):
	async def test_batches_generation_payloads_outside_request_path(self):
		requests = []

		async def handler(request: httpx.Request) -> httpx.Response:
			requests.append(json.loads(request.content))
			await asyncio.sleep(0.05)
			return httpx.Response(200, json={"name": "accepted"})

		async with httpx.AsyncClient(
			base_url="http://langfuse.test", transport=httpx.MockTransport(handler),
		) as client:
			dispatcher = LangfuseGenerationDispatcher(_settings(), async_client=client)
			await dispatcher.start()
			await asyncio.wait_for(dispatcher.arecord_generation(**_generation(0)), timeout=0.01)
			await asyncio.wait_for(dispatcher.arecord_generation(**_generation(1)), timeout=0.01)
			await asyncio.wait_for(dispatcher.queue.join(), timeout=1)
			await dispatcher.stop()

		self.assertEqual(len(requests), 1)
		self.assertEqual(len(requests[0]["resourceSpans"]), 2)
		self.assertEqual(dispatcher.metrics.sent_total, 2)
		self.assertEqual(dispatcher.metrics.dropped_total, 0)

	async def test_full_queue_drops_without_waiting(self):
		async with httpx.AsyncClient(
			base_url="http://langfuse.test",
			transport=httpx.MockTransport(lambda _request: httpx.Response(200)),
		) as client:
			dispatcher = LangfuseGenerationDispatcher(
				_settings(langfuse_queue_capacity=1), async_client=client,
			)
			self.assertTrue(await dispatcher.arecord_generation(**_generation(0)))
			self.assertFalse(await dispatcher.arecord_generation(**_generation(1)))

		self.assertEqual(dispatcher.metrics.dropped_total, 1)
		self.assertEqual(dispatcher.snapshot()["last_error"], "queue_full")

	async def test_retries_then_reports_success(self):
		attempts = 0

		async def handler(_request: httpx.Request) -> httpx.Response:
			nonlocal attempts
			attempts += 1
			return httpx.Response(503 if attempts < 3 else 200)

		async with httpx.AsyncClient(
			base_url="http://langfuse.test", transport=httpx.MockTransport(handler),
		) as client:
			dispatcher = LangfuseGenerationDispatcher(_settings(), async_client=client)
			await dispatcher.start()
			self.assertTrue(await dispatcher.arecord_generation(**_generation(0)))
			await asyncio.wait_for(dispatcher.queue.join(), timeout=2)
			await dispatcher.stop()

		self.assertEqual(attempts, 3)
		self.assertEqual(dispatcher.metrics.retry_total, 2)
		self.assertEqual(dispatcher.metrics.sent_total, 1)
		self.assertEqual(dispatcher.metrics.batch_failure_total, 0)
