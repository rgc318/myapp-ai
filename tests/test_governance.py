import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import httpx

from myapp_ai.config import Settings
from myapp_ai.governance import discover_models, validate_policy, validate_vector_release


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
		"embedding_model": "erp-embedding",
		"qdrant_url": "http://qdrant.test",
	}
	values.update(overrides)
	return Settings(**values)


class TestGovernance(TestCase):
	def test_model_discovery_exposes_only_configured_capability_aliases(self):
		def handler(request: httpx.Request):
			self.assertEqual(request.url.path, "/v1/models")
			return httpx.Response(200, json={"data": [{"id": "erp-fast-chat"}, {"id": "erp-embedding"}, {"id": "unused"}]})

		models = discover_models(_settings(), transport=httpx.MockTransport(handler))

		self.assertEqual([model["model_alias"] for model in models], ["erp-fast-chat", "erp-embedding"])
		self.assertEqual(models[0]["capability"], "fast_chat")
		self.assertEqual(models[1]["capability"], "embedding")
		self.assertEqual({model["status"] for model in models}, {"active"})

	@patch("myapp_ai.governance.discover_models")
	def test_policy_validation_requires_a_governed_full_live_gate(self, mock_discover):
		mock_discover.return_value = [{
			"model_alias": "erp-fast-chat", "capability": "fast_chat", "status": "active",
		}]

		result = validate_policy(_settings(governance_live_gate_report_path=""), {
			"scenario": "general", "capability": "fast_chat",
			"primary_model_alias": "erp-fast-chat", "fallback_model_aliases": [],
		})

		self.assertFalse(result["release_gate_eligible"])
		self.assertTrue(any("report path" in error for error in result["errors"]))
		self.assertTrue(result["evaluation"]["offline"]["summary"]["passed"])

	@patch("myapp_ai.governance.discover_models")
	def test_policy_validation_accepts_matching_passed_full_live_gate(self, mock_discover):
		mock_discover.return_value = [{
			"model_alias": "erp-fast-chat", "capability": "fast_chat", "status": "active",
		}]
		with TemporaryDirectory() as directory:
			path = Path(directory) / "live-gate.json"
			path.write_text(json.dumps({
				"schema_version": "myapp-ai-eval-report-v1",
				"run_id": "live-run-1",
				"mode": "live",
				"environment": "staging",
				"dataset": {"name": "core.v1.jsonl", "version": "v1", "case_count": 21},
				"summary": {
					"passed": True, "gate_scope": "full", "release_gate_eligible": True,
					"threshold_failures": [],
				},
				"cases": [{"attempts": [{"model_alias": "erp-fast-chat"}]}],
			}), encoding="utf-8")

			result = validate_policy(_settings(governance_live_gate_report_path=str(path)), {
				"scenario": "general", "capability": "fast_chat",
				"primary_model_alias": "erp-fast-chat", "fallback_model_aliases": [],
			})

		self.assertTrue(result["release_gate_eligible"])
		self.assertEqual(result["errors"], [])
		self.assertEqual(result["evaluation"]["governed_report"]["run_id"], "live-run-1")

	def test_vector_release_validation_requires_matching_full_gate_report(self):
		with TemporaryDirectory() as directory:
			path = Path(directory) / "embedding-gate.json"
			path.write_text(json.dumps({
				"schema_version": "myapp-ai-embedding-release-report-v1",
				"release_code": "products-v2",
				"embedding_model": "erp-embedding-v2",
				"collection": "myapp-products-v2",
				"index_version": "product-semantic-v2",
				"summary": {
					"passed": True,
					"gate_scope": "full",
					"release_gate_eligible": True,
					"threshold_failures": [],
				},
			}), encoding="utf-8")

			result = validate_vector_release(
				_settings(governance_embedding_gate_report_path=str(path)),
				{
					"release_code": "products-v2",
					"embedding_model": "erp-embedding-v2",
					"collection": "myapp-products-v2",
					"index_version": "product-semantic-v2",
				},
			)

		self.assertTrue(result["release_gate_eligible"])
		self.assertEqual(result["errors"], [])
