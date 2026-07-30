from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

import httpx

from myapp_ai.agent_tool_client import AgentToolClient, RuntimeEventPersistenceError
from myapp_ai.config import Settings


def _settings() -> Settings:
	return Settings(
		litellm_base_url="http://litellm.test",
		litellm_api_key="test-key",
		model="erp-agent-chat",
		reasoning_effort="none",
		service_token="service-token",
		timeout_seconds=10,
		max_messages=20,
		max_message_chars=8000,
		frappe_base_url="http://frappe.test",
		frappe_site_host="localhost",
	)


class TestAgentToolClient(IsolatedAsyncioTestCase):
	async def test_runtime_event_retries_transient_frappe_write_conflict(self):
		requests = []

		async def handler(request: httpx.Request) -> httpx.Response:
			requests.append(request)
			if len(requests) == 1:
				return httpx.Response(
					417,
					json={
						"exc_type": "QueryDeadlockError",
						"exception": "Record has changed since last read",
					},
				)
			return httpx.Response(200, json={"message": {"event_id": "runtime:checkpoint:1"}})

		async with httpx.AsyncClient(
			transport=httpx.MockTransport(handler), base_url="http://frappe.test",
		) as http:
			with patch("myapp_ai.agent_tool_client.asyncio.sleep", new=AsyncMock()) as sleep:
				result = await AgentToolClient(_settings(), async_client=http).record_runtime_event(
					run_id="AI-RUN-1", event_id="runtime:checkpoint:1",
					step_type="checkpoint", status="completed", data={}, checkpoint=None,
					capability_token="x" * 40,
				)

		self.assertEqual(result["event_id"], "runtime:checkpoint:1")
		self.assertEqual(len(requests), 2)
		sleep.assert_awaited_once()

	async def test_runtime_event_does_not_retry_non_transient_validation_error(self):
		requests = []

		async def handler(request: httpx.Request) -> httpx.Response:
			requests.append(request)
			return httpx.Response(
				417,
				json={"exc_type": "ValidationError", "exception": "Agent 检查点格式不正确。"},
			)

		async with httpx.AsyncClient(
			transport=httpx.MockTransport(handler), base_url="http://frappe.test",
		) as http:
			with self.assertRaises(RuntimeEventPersistenceError) as raised:
				await AgentToolClient(_settings(), async_client=http).record_runtime_event(
					run_id="AI-RUN-1", event_id="runtime:checkpoint:1",
					step_type="checkpoint", status="completed", data={}, checkpoint=None,
					capability_token="x" * 40,
				)

		self.assertEqual(len(requests), 1)
		self.assertEqual(raised.exception.status_code, 417)
		self.assertEqual(raised.exception.exc_type, "ValidationError")
		self.assertIn("Agent 检查点格式不正确", str(raised.exception))
