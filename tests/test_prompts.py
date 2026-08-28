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
			"general", "intent_parse", "product_search", "order_query", "report_summary",
			"sales_order_draft", "purchase_order_draft", "inventory_adjustment_draft",
			"product_setup_draft",
		}
		self.assertEqual(set(PROMPT_REGISTRY), expected)
		self.assertEqual(set(prompt_versions()), expected)
		self.assertEqual(PROMPT_REGISTRY["intent_parse"].version, "erp-intent-v6")
		self.assertIn("conversation_state", PROMPT_REGISTRY["intent_parse"].text)
		self.assertIn("当前消息优先级最高", PROMPT_REGISTRY["intent_parse"].text)
		self.assertIn("不能把“这个商品", PROMPT_REGISTRY["intent_parse"].text)
		self.assertIn("最短可靠身份", PROMPT_REGISTRY["intent_parse"].text)
		self.assertIn("不得因为图片身份不清晰而沿用", PROMPT_REGISTRY["intent_parse"].text)
		self.assertIn("红色可乐饮料", PROMPT_REGISTRY["intent_parse"].text)
		self.assertIn("product_hypotheses", PROMPT_REGISTRY["intent_parse"].text)
		self.assertEqual(PROMPT_REGISTRY["sales_order_draft"].version, "sales-order-draft-v4")
		self.assertIn("未明确时返回 null", PROMPT_REGISTRY["sales_order_draft"].text)
		self.assertIn("3 个", PROMPT_REGISTRY["sales_order_draft"].text)
		self.assertIn("没有引用待修改订单", PROMPT_REGISTRY["sales_order_draft"].text)
		self.assertEqual(PROMPT_REGISTRY["purchase_order_draft"].version, "purchase-order-draft-v4")
		self.assertIn("没有引用待修改订单", PROMPT_REGISTRY["purchase_order_draft"].text)
		self.assertIn("字段缺失说明不是备注", PROMPT_REGISTRY["purchase_order_draft"].text)
		self.assertEqual(PROMPT_REGISTRY["general"].version, "erp-readonly-v11")
		self.assertIn("不能声称“唯一匹配”", PROMPT_REGISTRY["general"].text)
		self.assertIn("resolution_status=resolved", PROMPT_REGISTRY["general"].text)
		self.assertIn("禁止退回通用欢迎语", PROMPT_REGISTRY["general"].text)
		self.assertIn("中文单据类型", PROMPT_REGISTRY["general"].text)

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
