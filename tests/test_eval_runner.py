import json
import os
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch

from myapp_ai.config import Settings
from myapp_ai.evals.dataset import EvalConfigurationError, load_dataset, load_thresholds
from myapp_ai.evals.runner import main, run_evaluation


def _settings() -> Settings:
	return Settings(
		litellm_base_url="http://unused.test", litellm_api_key="",
		model="erp-fast-chat", reasoning_effort="none", service_token="service-token",
		timeout_seconds=10, max_messages=20, max_message_chars=8000,
	)


class TestEvalRunner(TestCase):
	def test_offline_core_dataset_passes_without_network_or_raw_content(self):
		report = run_evaluation(
			settings=_settings(), mode="offline", dataset=load_dataset("core"),
			thresholds=load_thresholds("thresholds"),
		)

		self.assertTrue(report["summary"]["passed"])
		self.assertEqual(report["dataset"]["case_count"], 21)
		self.assertEqual(report["summary"]["metrics"]["schema_valid_rate"], 1.0)
		self.assertEqual(report["summary"]["metrics"]["safety_pass_rate"], 1.0)
		self.assertEqual(report["summary"]["metrics"]["structured_field_accuracy"], 1.0)
		self.assertEqual(report["summary"]["gate_scope"], "full")
		self.assertTrue(report["summary"]["release_gate_eligible"])
		self.assertFalse(report["content_included"])
		self.assertTrue(all(
			"output" not in attempt
			for case in report["cases"]
			for attempt in case["attempts"]
		))
		self.assertTrue(all(
			not attempt["observability_synced"]
			for case in report["cases"]
			for attempt in case["attempts"]
		))

	def test_content_is_only_included_when_explicitly_requested(self):
		report = run_evaluation(
			settings=_settings(), mode="offline", dataset=load_dataset("core"),
			thresholds=load_thresholds("thresholds"), include_content=True,
			case_ids={"chat.write_action_refusal"},
		)

		attempt = report["cases"][0]["attempts"][0]
		self.assertIn("output", attempt)
		self.assertIn("不能", attempt["output"])

	def test_offline_runner_returns_all_three_structured_draft_types(self):
		case_ids = {
			"draft.sales.complete",
			"draft.purchase.complete",
			"draft.inventory.set_target",
		}
		report = run_evaluation(
			settings=_settings(), mode="offline", dataset=load_dataset("core"),
			thresholds=load_thresholds("thresholds"), include_content=True,
			case_ids=case_ids,
		)

		self.assertEqual({case["id"] for case in report["cases"]}, case_ids)
		self.assertTrue(all(case["passed"] for case in report["cases"]))
		outputs = {case["scenario"]: case["attempts"][0]["output"] for case in report["cases"]}
		self.assertEqual(outputs["sales_order_draft"]["customer_query"], "华东演示客户")
		self.assertEqual(outputs["purchase_order_draft"]["supplier_query"], "演示供应商甲")
		self.assertEqual(outputs["inventory_adjustment_draft"]["adjustment_type"], "set_target")

	def test_cli_returns_two_when_live_mode_is_not_explicitly_enabled(self):
		with patch.dict(os.environ, {"MYAPP_AI_ENABLE_LIVE_EVALS": "0"}, clear=False):
			self.assertEqual(main(["--mode", "live"]), 2)

	def test_cli_writes_machine_readable_offline_report(self):
		with tempfile.TemporaryDirectory() as directory:
			output = os.path.join(directory, "report.json")
			exit_code = main([
				"--mode", "offline", "--case", "chat.write_action_refusal", "--output", output,
			])
			payload = json.loads(Path(output).read_text(encoding="utf-8"))

		self.assertEqual(exit_code, 0)
		self.assertEqual(payload["schema_version"], "myapp-ai-eval-report-v1")
		self.assertTrue(payload["summary"]["passed"])
		self.assertEqual(payload["summary"]["gate_scope"], "partial")
		self.assertFalse(payload["summary"]["release_gate_eligible"])
		self.assertEqual(payload["summary"]["metrics"]["critical_case_pass_rate"], 1.0)
		self.assertIsNone(payload["summary"]["metrics"]["normal_case_pass_rate"])
		self.assertIsNone(payload["summary"]["metrics"]["structured_field_accuracy"])

	def test_mixed_known_and_unknown_case_ids_are_rejected(self):
		with self.assertRaisesRegex(EvalConfigurationError, "Unknown evaluation case ids"):
			run_evaluation(
				settings=_settings(), mode="offline", dataset=load_dataset("core"),
				thresholds=load_thresholds("thresholds"),
				case_ids={"chat.write_action_refusal", "missing.case"},
			)

	def test_cli_returns_two_for_unknown_case_ids(self):
		self.assertEqual(main([
			"--mode", "offline", "--case", "chat.write_action_refusal",
			"--case", "missing.case",
		]), 2)
