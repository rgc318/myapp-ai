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
