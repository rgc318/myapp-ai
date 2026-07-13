from unittest import TestCase

from myapp_ai.evals.dataset import load_dataset, load_thresholds
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
