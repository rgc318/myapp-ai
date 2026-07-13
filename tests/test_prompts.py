from unittest import TestCase

from myapp_ai.prompts import (
	PROMPT_REGISTRY,
	PromptVersionMismatchError,
	get_prompt_spec,
	prompt_versions,
	with_effective_prompt,
)
from myapp_ai.schemas import ChatMessage, ChatRequest


class TestPromptRegistry(TestCase):
	def test_registry_covers_every_supported_scenario(self):
		expected = {
			"general", "product_search", "order_query", "report_summary",
			"sales_order_draft", "purchase_order_draft", "inventory_adjustment_draft",
		}
		self.assertEqual(set(PROMPT_REGISTRY), expected)
		self.assertEqual(set(prompt_versions()), expected)

	def test_draft_prompt_version_rejects_stale_client_version(self):
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="采购两箱相机")],
			user="test@example.com", scenario="purchase_order_draft",
			prompt_version="erp-readonly-v3",
		)

		with self.assertRaisesRegex(PromptVersionMismatchError, "Prompt version mismatch"):
			with_effective_prompt(request)

	def test_endpoint_can_force_its_structured_scenario(self):
		request = ChatRequest(
			messages=[ChatMessage(role="user", content="调整库存")],
			user="test@example.com", scenario="general",
		)

		resolved = with_effective_prompt(request, scenario="inventory_adjustment_draft")

		self.assertEqual(resolved.scenario, "inventory_adjustment_draft")
		self.assertEqual(resolved.prompt_version, "inventory-adjustment-draft-v2")
		self.assertEqual(get_prompt_spec(resolved.scenario).capability, "erp-structured")
