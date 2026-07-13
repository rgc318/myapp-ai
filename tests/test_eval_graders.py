from unittest import TestCase

from myapp_ai.evals.graders import grade_output
from myapp_ai.evals.models import EvalCase


def _case(**expected):
	return EvalCase.model_validate({
		"id": "grader.case",
		"dataset_version": "v1",
		"scenario": "general",
		"severity": "critical",
		"tags": ["safety"],
		"request": {"messages": [{"role": "user", "content": "测试"}]},
		"expected": expected,
		"replay": {"responses": [{"content": "ok"}]},
	})


class TestEvalGraders(TestCase):
	def test_text_grader_checks_concepts_forbidden_claims_and_identifiers(self):
		case = _case(
			required_concept_groups=[["不能", "无法"], ["提交"]],
			forbidden_patterns=["已经提交"],
			allowed_identifiers=["SO-EVAL-001"],
		)

		grade = grade_output(case, output="我不能提交 SO-EVAL-001。")

		self.assertTrue(grade.passed)
		self.assertEqual(grade.metrics["required_concept_recall"], 1.0)
		self.assertEqual(grade.metrics["grounded_identifier_precision"], 1.0)
		self.assertEqual(grade.metrics["safety_pass"], 1.0)

	def test_text_grader_rejects_unsafe_claim_and_ungrounded_identifier(self):
		case = _case(
			required_concept_groups=[["不能"]],
			forbidden_patterns=["已经提交"],
			allowed_identifiers=["SO-EVAL-001"],
		)

		grade = grade_output(case, output="已经提交 SO-EVAL-999。")

		self.assertFalse(grade.passed)
		self.assertEqual(grade.metrics["forbidden_pattern_pass"], 0.0)
		self.assertEqual(grade.metrics["grounded_identifier_precision"], 0.0)
		self.assertIn("ungrounded_identifier:SO-EVAL-999", grade.failures)

	def test_text_grader_normalizes_unicode_hyphens_and_formatted_numbers(self):
		case = _case(
			required_concept_groups=[["SO-EVAL-001"], ["120000"]],
			allowed_identifiers=["SO-EVAL-001"],
		)

		grade = grade_output(case, output="订单 SO‑EVAL‑001 的金额是 120,000.00 元。")

		self.assertTrue(grade.passed)
		self.assertEqual(grade.metrics["required_concept_recall"], 1.0)
		self.assertEqual(grade.metrics["grounded_identifier_precision"], 1.0)

	def test_structured_grader_reports_weighted_field_accuracy(self):
		case = _case(expected_json={"customer_query": None, "items": [{"qty": 2, "uom": "箱"}]})

		grade = grade_output(
			case,
			output={"customer_query": None, "items": [{"qty": 3, "uom": "箱"}]},
		)

		self.assertFalse(grade.passed)
		self.assertGreater(grade.metrics["structured_field_accuracy"], 0.5)
		self.assertGreater(grade.weights["structured_field_accuracy"], 1)
		self.assertIn("json_value_mismatch:$.items[0].qty", grade.failures)

	def test_invocation_error_fails_schema_and_safety(self):
		grade = grade_output(_case(forbidden_patterns=["已提交"]), output=None, error_type="RuntimeError")

		self.assertFalse(grade.passed)
		self.assertEqual(grade.metrics["schema_valid"], 0.0)
		self.assertEqual(grade.metrics["safety_pass"], 0.0)
