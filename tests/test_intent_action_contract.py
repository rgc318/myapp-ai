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

	def test_prompt_declares_action_and_reset_semantics(self):
		prompt = get_prompt_spec("intent_parse")
		self.assertEqual(prompt.version, "erp-intent-v7")
		self.assertIn("不能为了适配场景把动作改写", prompt.text)
		self.assertIn("query_context_operations", prompt.text)
