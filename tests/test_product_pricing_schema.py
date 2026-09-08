from unittest import TestCase

from pydantic import ValidationError

from myapp_ai.prompts import PROMPT_REGISTRY
from myapp_ai.schemas import ProductSetupPatchCandidate


class TestProductPricingSchema(TestCase):
	def test_per_unit_prices_and_unknown_packaging_are_preserved(self):
		patch = ProductSetupPatchCandidate.model_validate({"stock_uom": "Box", "prices": [
			{"price_list": "Retail", "rate": 3.5, "uom": "Bottle", "interpretation": "inferred", "evidence": "3.5元每瓶"},
			{"price_list": "Wholesale", "rate": 30, "uom": "Box", "interpretation": "inferred"},
			{"price_list": "Standard Buying", "rate": 25, "uom": "Box"}],
			"uom_relations": [{"from_uom": "Bottle", "from_qty": None, "to_uom": "Box", "to_qty": 1}]})
		self.assertEqual(len(patch.prices), 3)
		self.assertIsNone(patch.uom_relations[0].from_qty)
		self.assertEqual(patch.model_dump()["prices"][0]["uom"], "Bottle")

	def test_invalid_rate_and_unknown_price_role_rejected(self):
		for row in ({"price_list": "Retail", "rate": float("inf")}, {"price_list": "Retail", "rate": -1}, {"price_list": "Special", "rate": 1}):
			with self.subTest(row=row), self.assertRaises(ValidationError):
				ProductSetupPatchCandidate.model_validate({"prices": [row]})

	def test_prompt_contract_and_unsupported_conditions(self):
		self.assertEqual(PROMPT_REGISTRY["product_setup_draft"].version, "product-setup-draft-v8")
		self.assertIn("pricing_unresolved", PROMPT_REGISTRY["product_setup_draft"].text)
		self.assertEqual(ProductSetupPatchCandidate(pricing_unresolved=["数量阶梯价格待处理"]).pricing_unresolved, ["数量阶梯价格待处理"])
