import json
import tempfile
from pathlib import Path
from unittest import TestCase

from myapp_ai.evals.dataset import EvalConfigurationError, load_dataset, load_thresholds
from myapp_ai.prompts import PROMPT_REGISTRY


class TestEvalDataset(TestCase):
	def test_core_dataset_is_versioned_unique_and_high_value(self):
		bundle = load_dataset("core")

		self.assertEqual(bundle.version, "ai-core-v1")
		self.assertGreaterEqual(len(bundle.cases), 20)
		self.assertEqual(len(bundle.cases), len({case.id for case in bundle.cases}))
		self.assertEqual({case.scenario for case in bundle.cases}, set(PROMPT_REGISTRY))
		self.assertGreaterEqual(sum(case.severity == "critical" for case in bundle.cases), 5)
		self.assertTrue(all(case.replay.responses for case in bundle.cases))

	def test_thresholds_enforce_critical_schema_and_safety_contracts(self):
		thresholds = load_thresholds("thresholds")

		for mode in (thresholds.offline, thresholds.live):
			self.assertEqual(mode.critical_case_pass_rate, 1.0)
			self.assertEqual(mode.schema_valid_rate, 1.0)
			self.assertEqual(mode.safety_pass_rate, 1.0)
			self.assertEqual(mode.structured_field_accuracy, 0.95)
			self.assertEqual(mode.normal_case_pass_rate, 0.9)

	def test_agent_cases_separate_expected_trajectory_from_runtime_replay(self):
		agent_cases = [case for case in load_dataset("core").cases if "agent" in case.tags]

		self.assertTrue(agent_cases)
		for case in agent_cases:
			self.assertEqual(set(case.modes), {"offline", "live"})
			self.assertTrue(case.expected.expected_trajectory)
			self.assertFalse(any(response.trajectory for response in case.replay.responses))
			formal_calls = [
				call
				for response in case.replay.responses
				for choice in ((response.body or {}).get("choices") or [])
				for call in ((choice.get("message") or {}).get("tool_calls") or [])
			]
			self.assertTrue(formal_calls)
			self.assertGreaterEqual(len(case.replay.tool_results), len(formal_calls))

		observed_tools = {
			str(step.get("tool") or "")
			for case in agent_cases
			for step in case.expected.expected_trajectory
		}
		self.assertEqual(
			observed_tools,
			{"search_products", "query_business_documents", "get_business_report"},
		)
		self.assertTrue(any(len(case.expected.expected_trajectory) > 1 for case in agent_cases))
		self.assertTrue(any(len(case.request.messages) > 1 for case in agent_cases))

	def test_agent_case_rejects_replay_trajectory_as_actual(self):
		payload = {
			"id": "agent.invalid-trajectory", "dataset_version": "ai-core-v1",
			"scenario": "general", "tags": ["agent"],
			"request": {"messages": [{"role": "user", "content": "查商品"}], "company": "测试公司"},
			"expected": {"expected_trajectory": [{"type": "tool", "tool": "search_products"}]},
			"replay": {
				"responses": [{
					"body": {"choices": [{"message": {"tool_calls": [{
						"id": "call-1", "type": "function",
						"function": {"name": "search_products", "arguments": "{}"},
					}]}}]},
					"trajectory": [{"type": "tool", "tool": "search_products"}],
				}],
				"tool_results": [{"tool": "search_products", "status": "not_found"}],
			},
		}
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "invalid.jsonl"
			path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
			with self.assertRaisesRegex(EvalConfigurationError, "cannot use replay response trajectory"):
				load_dataset(str(path))
