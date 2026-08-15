import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import httpx

from myapp_ai.config import Settings
from myapp_ai.governance import (
	VISION_PROBES,
	check_model_availability,
	discover_models,
	validate_policy,
	validate_vector_release,
)
from myapp_ai.release_provenance import prompt_manifest, tool_manifest

RUNTIME_REVISION = "a" * 40


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


def _gate_report(
	*, mode: str, model_aliases: list[str], runtime_revision: str = RUNTIME_REVISION,
) -> dict:
	return {
		"schema_version": "myapp-ai-eval-report-v2",
		"run_id": f"{mode}-run-1",
		"mode": mode,
		"environment": "staging",
		"provenance": {
			"runtime_revision": runtime_revision,
			"prompt_manifest": prompt_manifest(),
			"tool_manifest": tool_manifest(),
			"requested_model_aliases": model_aliases,
		},
		"dataset": {
			"name": "core.v1.jsonl", "version": "v1", "sha256": "dataset-sha", "case_count": 32,
		},
		"summary": {
			"passed": True, "gate_scope": "full", "release_gate_eligible": True,
			"threshold_failures": [],
		},
		"cases": [{"attempts": [
			{"configured_model_alias": alias, "model_alias": alias}
			for alias in model_aliases
		]}],
	}


class TestGovernance(TestCase):
	def test_model_discovery_exposes_all_litellm_visible_aliases(self):
		def handler(request: httpx.Request):
			self.assertEqual(request.url.path, "/v1/models")
			return httpx.Response(200, json={"data": [{"id": "erp-fast-chat"}, {"id": "erp-embedding"}, {"id": "unused"}]})

		models = discover_models(_settings(), transport=httpx.MockTransport(handler))

		self.assertEqual(
			[model["model_alias"] for model in models],
			["erp-fast-chat", "unused", "erp-embedding"],
		)
		self.assertEqual(models[0]["capability"], "fast_chat")
		self.assertEqual(models[1]["capability"], "fast_chat")
		self.assertEqual(models[2]["capability"], "embedding")
		self.assertEqual({model["status"] for model in models}, {"active"})
		self.assertEqual({model["last_health_status"] for model in models}, {"listed"})
		self.assertFalse(models[0]["supports_tools"])

	def test_model_availability_checks_chat_and_embedding_endpoints(self):
		def handler(request: httpx.Request):
			if request.url.path == "/v1/models":
				return httpx.Response(200, json={"data": [{"id": "erp-fast-chat"}, {"id": "erp-embedding"}]})
			if request.url.path == "/v1/chat/completions":
				payload = json.loads(request.content)
				if payload.get("tools"):
					return httpx.Response(200, json={
						"model": "provider-chat",
						"choices": [{"message": {"role": "assistant", "tool_calls": [{
							"id": "probe-1", "type": "function",
							"function": {"name": "capability_probe", "arguments": "{\"value\":\"ok\"}"},
						}]}}],
					})
				content = ((payload.get("messages") or [{}])[0]).get("content")
				if isinstance(content, list):
					self.assertEqual(content[1]["type"], "image_url")
					image_url = content[1]["image_url"]["url"]
					expected = next(color for color, probe_url in VISION_PROBES if probe_url == image_url)
					return httpx.Response(200, json={
						"model": "provider-chat",
						"choices": [{"message": {"role": "assistant", "content": expected}}],
					})
				return httpx.Response(200, json={
					"model": "provider-chat", "choices": [{"message": {"role": "assistant", "content": "OK"}}],
				})
			if request.url.path == "/v1/embeddings":
				return httpx.Response(200, json={"model": "provider-embedding", "data": [{"embedding": [0.1]}]})
			raise AssertionError(request.url.path)

		result = check_model_availability(_settings(), transport=httpx.MockTransport(handler))

		self.assertEqual(result["checked_count"], 2)
		self.assertEqual(result["available_count"], 2)
		self.assertEqual(result["unavailable_count"], 0)
		self.assertEqual([item["available"] for item in result["items"]], [True, True])
		self.assertTrue(result["items"][0]["supports_tools"])
		self.assertTrue(result["items"][0]["supports_vision"])
		self.assertFalse(result["items"][1]["supports_tools"])
		self.assertFalse(result["items"][1]["supports_vision"])

	def test_text_only_model_cannot_pass_vision_probe_by_repeating_a_prompt_answer(self):
		def handler(request: httpx.Request):
			if request.url.path == "/v1/models":
				return httpx.Response(200, json={"data": [{"id": "erp-fast-chat"}]})
			payload = json.loads(request.content)
			if payload.get("tools"):
				return httpx.Response(200, json={"choices": [{"message": {"content": "no tool"}}]})
			content = ((payload.get("messages") or [{}])[0]).get("content")
			if isinstance(content, list):
				self.assertNotIn("red", content[0]["text"].casefold())
				self.assertNotIn("blue", content[0]["text"].casefold())
				return httpx.Response(200, json={"choices": [{"message": {"content": "red"}}]})
			return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

		result = check_model_availability(_settings(), transport=httpx.MockTransport(handler))

		self.assertTrue(result["items"][0]["available"])
		self.assertFalse(result["items"][0]["supports_vision"])
		self.assertEqual(result["items"][0]["vision_error_code"], "VISION_PROBE_MISMATCH")

	def test_model_availability_keeps_text_model_available_when_vision_probe_fails(self):
		def handler(request: httpx.Request):
			if request.url.path == "/v1/models":
				return httpx.Response(200, json={"data": [{"id": "erp-fast-chat"}]})
			payload = json.loads(request.content)
			if payload.get("tools"):
				return httpx.Response(200, json={"choices": [{"message": {"content": "no tool"}}]})
			content = ((payload.get("messages") or [{}])[0]).get("content")
			if isinstance(content, list):
				return httpx.Response(400, json={"error": {"message": "image input unsupported"}})
			return httpx.Response(200, json={
				"model": "provider-chat", "choices": [{"message": {"role": "assistant", "content": "OK"}}],
			})

		result = check_model_availability(_settings(), transport=httpx.MockTransport(handler))

		self.assertEqual(result["available_count"], 1)
		self.assertTrue(result["items"][0]["available"])
		self.assertFalse(result["items"][0]["supports_vision"])
		self.assertEqual(result["items"][0]["vision_error_code"], "PROVIDER_HTTP_400")

	def test_model_availability_reports_provider_failure_without_response_content(self):
		def handler(request: httpx.Request):
			if request.url.path == "/v1/models":
				return httpx.Response(200, json={"data": [{"id": "erp-fast-chat"}]})
			return httpx.Response(429, json={"error": {"message": "provider secret detail"}})

		result = check_model_availability(_settings(), transport=httpx.MockTransport(handler))

		self.assertEqual(result["available_count"], 0)
		self.assertEqual(result["items"][0]["error_code"], "PROVIDER_HTTP_429")
		self.assertNotIn("secret", json.dumps(result))

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
		self.assertIsNone(result["evaluation"]["offline"])

	@patch("myapp_ai.governance.discover_models")
	def test_policy_validation_accepts_matching_passed_full_live_gate(self, mock_discover):
		mock_discover.return_value = [{
			"model_alias": "erp-fast-chat", "capability": "fast_chat", "status": "active",
		}]
		with TemporaryDirectory() as directory:
			offline_path = Path(directory) / "offline-gate.json"
			live_path = Path(directory) / "live-gate.json"
			offline_path.write_text(
				json.dumps(_gate_report(mode="offline", model_aliases=["offline-replay-model"])),
				encoding="utf-8",
			)
			live_path.write_text(
				json.dumps(_gate_report(mode="live", model_aliases=["erp-fast-chat"])),
				encoding="utf-8",
			)

			result = validate_policy(_settings(
				runtime_revision=RUNTIME_REVISION,
				governance_offline_gate_report_path=str(offline_path),
				governance_live_gate_report_path=str(live_path),
			), {
				"scenario": "general", "capability": "fast_chat",
				"primary_model_alias": "erp-fast-chat", "fallback_model_aliases": [],
			})

		self.assertTrue(result["release_gate_eligible"])
		self.assertEqual(result["errors"], [])
		self.assertEqual(result["evaluation"]["governed_report"]["run_id"], "live-run-1")

	@patch("myapp_ai.governance.discover_models")
	def test_policy_validation_rejects_stale_runtime_and_model_bound_reports(self, mock_discover):
		mock_discover.return_value = [
			{"model_alias": alias, "capability": "fast_chat", "status": "active"}
			for alias in ("erp-fast-chat", "erp-safe-fallback")
		]
		with TemporaryDirectory() as directory:
			offline_path = Path(directory) / "offline-gate.json"
			live_path = Path(directory) / "live-gate.json"
			offline_path.write_text(
				json.dumps(_gate_report(
					mode="offline", model_aliases=["offline-replay-model"], runtime_revision="b" * 40,
				)),
				encoding="utf-8",
			)
			live_path.write_text(
				json.dumps(_gate_report(
					mode="live", model_aliases=["erp-fast-chat"], runtime_revision="b" * 40,
				)),
				encoding="utf-8",
			)
			result = validate_policy(_settings(
				runtime_revision="c" * 40,
				governance_offline_gate_report_path=str(offline_path),
				governance_live_gate_report_path=str(live_path),
			), {
				"scenario": "general", "capability": "fast_chat",
				"primary_model_alias": "erp-fast-chat",
				"fallback_model_aliases": ["erp-safe-fallback"],
			})

		self.assertFalse(result["release_gate_eligible"])
		self.assertTrue(any("runtime revision" in error for error in result["errors"]))
		self.assertTrue(any("model aliases" in error for error in result["errors"]))

	@patch("myapp_ai.governance.discover_models")
	def test_policy_validation_rejects_stale_prompt_tool_and_dataset_provenance(self, mock_discover):
		mock_discover.return_value = [{
			"model_alias": "erp-fast-chat", "capability": "fast_chat", "status": "active",
		}]
		with TemporaryDirectory() as directory:
			offline = _gate_report(mode="offline", model_aliases=["offline-replay-model"])
			live = _gate_report(mode="live", model_aliases=["erp-fast-chat"])
			offline["provenance"]["prompt_manifest"] = {"sha256": "stale-prompt"}
			live["provenance"]["tool_manifest"] = {"sha256": "stale-tool"}
			live["dataset"] = {**live["dataset"], "sha256": "different-dataset"}
			offline_path = Path(directory) / "offline-gate.json"
			live_path = Path(directory) / "live-gate.json"
			offline_path.write_text(json.dumps(offline), encoding="utf-8")
			live_path.write_text(json.dumps(live), encoding="utf-8")

			result = validate_policy(_settings(
				runtime_revision=RUNTIME_REVISION,
				governance_offline_gate_report_path=str(offline_path),
				governance_live_gate_report_path=str(live_path),
			), {
				"scenario": "general", "capability": "fast_chat",
				"primary_model_alias": "erp-fast-chat", "fallback_model_aliases": [],
			})

		self.assertFalse(result["release_gate_eligible"])
		self.assertTrue(any("prompt manifest" in error for error in result["errors"]))
		self.assertTrue(any("tool manifest" in error for error in result["errors"]))
		self.assertTrue(any("same dataset" in error for error in result["errors"]))

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
