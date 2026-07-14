import hashlib
import json
from unittest import TestCase

import httpx

from myapp_ai.config import Settings
from myapp_ai.schemas import ProductVectorDocument, ProductVectorSearchRequest
from myapp_ai.vector_client import ProductVectorClient


def _settings() -> Settings:
	return Settings(
		litellm_base_url="http://litellm.test",
		litellm_api_key="test-key",
		model="erp-fast-chat",
		reasoning_effort="none",
		service_token="service-token",
		timeout_seconds=10,
		max_messages=20,
		max_message_chars=8000,
		embedding_model="erp-embedding",
		qdrant_url="http://qdrant.test",
		qdrant_collection="myapp-products-v1",
	)


class TestProductVectorClient(TestCase):
	def test_upsert_embeds_documents_creates_collection_and_omits_raw_text_payload(self):
		requests = []

		def handler(request: httpx.Request):
			payload = json.loads(request.content) if request.content else None
			requests.append((request.method, str(request.url), payload))
			if request.url.host == "litellm.test":
				return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]})
			if request.method == "GET":
				return httpx.Response(404, json={"status": "not found"})
			return httpx.Response(200, json={"result": {"status": "completed"}})

		text = "编码 ITEM-001；名称 蓝色包装饮料；用途 聚会整箱销售"
		document = ProductVectorDocument(
			item_code="ITEM-001",
			text=text,
			content_hash=hashlib.sha256(text.encode()).hexdigest(),
			index_version="product-semantic-v1",
			is_sales_item=1,
			is_purchase_item=1,
			company_scope=["*"],
		)
		count = ProductVectorClient(_settings(), transport=httpx.MockTransport(handler)).upsert([document])

		self.assertEqual(count, 1)
		self.assertEqual(requests[0][2], {"model": "erp-embedding", "input": [text]})
		self.assertEqual(requests[2][2], {"vectors": {"size": 3, "distance": "Cosine"}})
		self.assertEqual(
			{request[2]["field_name"] for request in requests[3:7]},
			{"disabled", "is_sales_item", "is_purchase_item", "is_stock_item"},
		)
		point_payload = requests[-1][2]["points"][0]["payload"]
		self.assertEqual(point_payload["item_code"], "ITEM-001")
		self.assertNotIn("text", point_payload)

	def test_search_applies_read_only_business_filters(self):
		requests = []

		def handler(request: httpx.Request):
			payload = json.loads(request.content) if request.content else None
			requests.append((request.method, str(request.url), payload))
			if request.url.host == "litellm.test":
				return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.4, 0.5]}]})
			return httpx.Response(200, json={
				"result": [{
					"id": "point-1", "score": 0.91,
					"payload": {
						"item_code": "ITEM-001",
						"content_hash": "a" * 64,
						"index_version": "product-semantic-v1",
					},
				}],
			})

		matches = ProductVectorClient(_settings(), transport=httpx.MockTransport(handler)).search(
			ProductVectorSearchRequest(query="适合聚会整箱卖的蓝色饮料", item_context="sales", limit=8)
		)

		self.assertEqual(matches[0].item_code, "ITEM-001")
		self.assertEqual(matches[0].score, 0.91)
		search_payload = requests[1][2]
		self.assertIn({"key": "disabled", "match": {"value": 0}}, search_payload["filter"]["must"])
		self.assertIn({"key": "is_sales_item", "match": {"value": 1}}, search_payload["filter"]["must"])

	def test_delete_is_idempotent_when_collection_does_not_exist(self):
		def handler(_request: httpx.Request):
			return httpx.Response(404, json={"status": "not found"})

		count = ProductVectorClient(_settings(), transport=httpx.MockTransport(handler)).delete(["ITEM-001"])

		self.assertEqual(count, 0)

	def test_status_reports_collection_counts_without_requiring_embedding_model(self):
		def handler(_request: httpx.Request):
			return httpx.Response(200, json={
				"result": {
					"points_count": 12,
					"indexed_vectors_count": 12,
					"config": {"params": {"vectors": {"size": 384, "distance": "Cosine"}}},
				},
			})

		status = ProductVectorClient(_settings(), transport=httpx.MockTransport(handler)).status()

		self.assertTrue(status["reachable"])
		self.assertTrue(status["collection_exists"])
		self.assertEqual(status["points_count"], 12)
		self.assertEqual(status["vector_size"], 384)

	def test_missing_vector_configuration_fails_closed_inside_internal_client(self):
		settings = _settings()
		settings = Settings(
			litellm_base_url=settings.litellm_base_url,
			litellm_api_key=settings.litellm_api_key,
			model=settings.model,
			reasoning_effort=settings.reasoning_effort,
			service_token=settings.service_token,
			timeout_seconds=settings.timeout_seconds,
			max_messages=settings.max_messages,
			max_message_chars=settings.max_message_chars,
		)

		with self.assertRaisesRegex(RuntimeError, "not configured"):
			ProductVectorClient(settings).search(ProductVectorSearchRequest(query="饮料"))
