import os
from unittest import TestCase
from unittest.mock import patch

from myapp_ai.config import get_settings


class TestConfig(TestCase):
	def test_service_token_is_required(self):
		with patch.dict(os.environ, {}, clear=True):
			with self.assertRaisesRegex(RuntimeError, "MYAPP_AI_SERVICE_TOKEN"):
				get_settings()

	def test_service_token_rejects_placeholders(self):
		for token in (
			"change-me-use-at-least-32-random-characters",
			"staging-ai-service-token-not-configured",
			"local-development-ai-service-token",
		):
			with self.subTest(token=token), patch.dict(
				os.environ, {"MYAPP_AI_SERVICE_TOKEN": token}, clear=True,
			):
				with self.assertRaisesRegex(RuntimeError, "MYAPP_AI_SERVICE_TOKEN"):
					get_settings()

	def test_service_token_accepts_a_high_entropy_value(self):
		with patch.dict(
			os.environ,
			{"MYAPP_AI_SERVICE_TOKEN": "0123456789abcdef0123456789abcdef"},
			clear=True,
		):
			settings = get_settings()

		self.assertEqual(settings.service_token, "0123456789abcdef0123456789abcdef")
		self.assertEqual(settings.max_context_tokens, 24000)
		self.assertEqual(settings.agent_run_timeout_seconds, 90)
		self.assertEqual(settings.agent_cancel_poll_seconds, 0.5)
		self.assertEqual(settings.agent_max_total_tokens, 60000)

	def test_agent_runtime_limits_are_bounded(self):
		with patch.dict(
			os.environ,
			{
				"MYAPP_AI_SERVICE_TOKEN": "0123456789abcdef0123456789abcdef",
				"MYAPP_AI_AGENT_RUN_TIMEOUT_SECONDS": "999",
				"MYAPP_AI_AGENT_CANCEL_POLL_SECONDS": "0.01",
				"MYAPP_AI_AGENT_MAX_TOTAL_TOKENS": "9999999",
			},
			clear=True,
		):
			settings = get_settings()

		self.assertEqual(settings.agent_run_timeout_seconds, 300)
		self.assertEqual(settings.agent_cancel_poll_seconds, 0.2)
		self.assertEqual(settings.agent_max_total_tokens, 500000)
