from unittest import TestCase

from pydantic import ValidationError

from myapp_ai.prompts import get_prompt_spec
from myapp_ai.schemas import IntentActionContract, QueryContextOperations


class TestIntentActionContract(TestCase):
	def test_delete_and_preserve_are_representable_without_update(self):
		contract = IntentActionContract(request_mode="execute_request", operations=["delete"],
			target_count=2, has_preserve_targets=True)
		self.assertEqual(contract.operations, ["delete"])
		self.assertTrue(contract.has_preserve_targets)

	def test_unknown_operation_and_clear_field_rejected(self):
		with self.assertRaises(ValidationError):
			IntentActionContract(request_mode="execute_request", operations=["arbitrary"])
		with self.assertRaises(ValidationError):
			QueryContextOperations(clear_fields=["company"])

	def test_lifecycle_target_evidence_and_preserve_survive_serialization(self):
		contract = IntentActionContract(request_mode="execute_request", operations=["delete"],
			target_count=1, has_preserve_targets=True,
			product_targets=[{"query": "可乐2", "evidence": "删除可乐2"}],
			preserve_product_targets=[{"query": "可乐", "evidence": "保留可乐"}])
		self.assertEqual(contract.model_dump()["product_targets"][0]["query"], "可乐2")
		self.assertEqual(contract.preserve_product_targets[0].evidence, "保留可乐")

	def test_lifecycle_targets_reject_empty_unknown_and_excess_fields(self):
		for target in [{"query": "", "evidence": "删除"}, {"query": "A", "evidence": ""},
			{"query": "A", "evidence": "删除A", "confirmed": True}]:
			with self.subTest(target=target), self.assertRaises(ValidationError):
				IntentActionContract(product_targets=[target])
		with self.assertRaises(ValidationError):
			IntentActionContract(product_targets=[{"query": "A", "evidence": "A"}] * 21)

	def test_prompt_declares_action_and_reset_semantics(self):
		prompt = get_prompt_spec("intent_parse")
		self.assertEqual(prompt.version, "erp-intent-v8")
		self.assertIn("不能为了适配场景把动作改写", prompt.text)
		self.assertIn("query_context_operations", prompt.text)
