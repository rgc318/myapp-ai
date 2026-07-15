import json
from unittest import TestCase

import httpx

from myapp_ai.retrieval_quality import load_retrieval_dataset, run_retrieval_quality


class TestRetrievalQuality(TestCase):
	def test_versioned_chinese_dataset_has_three_cases_for_each_reference_sku(self):
		dataset = load_retrieval_dataset()

		self.assertEqual(dataset["version"], "product-retrieval-zh-cn-v1")
		self.assertEqual(len(dataset["cases"]), 30)
		counts = {}
		for case in dataset["cases"]:
			counts[case["expected_item_code"]] = counts.get(case["expected_item_code"], 0) + 1
		self.assertEqual(counts, {f"SKU{index:03d}": 3 for index in range(1, 11)})

	def test_quality_runner_calculates_topk_and_exclusion_gate(self):
		dataset = load_retrieval_dataset()
		expected_by_query = {case["query"]: case["expected_item_code"] for case in dataset["cases"]}

		def handler(request: httpx.Request) -> httpx.Response:
			query = json.loads(request.content)["query"]
			expected = expected_by_query[query]
			return httpx.Response(200, json={
				"matches": [
					{"item_code": expected, "score": 0.99},
					{"item_code": "SKU999", "score": 0.5},
				],
				"embedding_model": "erp-embedding",
				"collection": "myapp-products-live",
			})

		with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://ai.test") as client:
			report = run_retrieval_quality(
				client=client, dataset=dataset, excluded_prefixes=("http-",),
			)

		self.assertTrue(report["summary"]["passed"])
		self.assertEqual(report["summary"]["top1_rate"], 1.0)
		self.assertEqual(report["summary"]["top3_rate"], 1.0)
		self.assertEqual(report["summary"]["excluded_candidate_count"], 0)

	def test_quality_runner_fails_closed_on_provider_error(self):
		dataset = {**load_retrieval_dataset(), "cases": load_retrieval_dataset()["cases"][:1]}
		with httpx.Client(
			transport=httpx.MockTransport(lambda _request: httpx.Response(502)),
			base_url="http://ai.test",
		) as client:
			report = run_retrieval_quality(client=client, dataset=dataset)

		self.assertFalse(report["summary"]["passed"])
		self.assertEqual(report["summary"]["provider_error_count"], 1)
		self.assertIn("provider_errors_present", report["summary"]["threshold_failures"])
