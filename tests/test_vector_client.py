import hashlib
import json
from unittest import IsolatedAsyncioTestCase, TestCase

from pydantic import ValidationError

import httpx

from myapp_ai.config import Settings
from myapp_ai.schemas import ProductVectorDocument, ProductVectorSearchRequest, ProductVectorUpsertRequest
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
	def test_upsert_schema_accepts_governed_128_document_batch(self):
		document = ProductVectorDocument(
			item_code="ITEM-001", text="synthetic", content_hash="a" * 64,
			index_version="product-semantic-v1",
		)
		request = ProductVectorUpsertRequest(documents=[document] * 128)
		self.assertEqual(len(request.documents), 128)
		with self.assertRaises(ValidationError):
			ProductVectorUpsertRequest(documents=[document] * 129)

	def test_batch_embedding_falls_back_to_bounded_parallel_single_requests(self):
		calls = []

		def handler(request: httpx.Request):
			payload = json.loads(request.content)
			calls.append(payload["input"])
			if len(payload["input"]) > 1:
				return httpx.Response(500, json={"error": "batch unsupported"})
			value = 1.0 if payload["input"][0] == "one" else 2.0
			return httpx.Response(200, json={"data": [{"index": 0, "embedding": [value, value]}]})

		client = ProductVectorClient(_settings(), transport=httpx.MockTransport(handler))
		vectors = client._embed(["one", "two"])

		self.assertEqual(vectors, [[1.0, 1.0], [2.0, 2.0]])
		self.assertEqual(client.last_embedding_mode, "parallel_single_fallback")
		self.assertEqual(len(calls), 3)

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

	def test_upsert_can_target_governed_embedding_model_and_candidate_collection(self):
		requests = []

		def handler(request: httpx.Request):
			payload = json.loads(request.content) if request.content else None
			requests.append((request.method, request.url.path, payload))
			if request.url.host == "litellm.test":
				return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})
			if request.method == "GET":
				return httpx.Response(404, json={"status": "not found"})
			return httpx.Response(200, json={"result": {"status": "completed"}})

		text = "候选向量文档"
		document = ProductVectorDocument(
			item_code="ITEM-002", text=text,
			content_hash=hashlib.sha256(text.encode()).hexdigest(),
			index_version="product-semantic-v2",
		)
		client = ProductVectorClient(
			_settings(), transport=httpx.MockTransport(handler),
			embedding_model="erp-embedding-v2", collection="myapp-products-v2",
		)

		client.upsert([document])

		self.assertEqual(requests[0][2]["model"], "erp-embedding-v2")
		self.assertTrue(any(path == "/collections/myapp-products-v2/points" for _, path, _ in requests))

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

	def test_alias_switch_is_atomic_and_reports_previous_collection(self):
		requests = []

		def handler(request: httpx.Request):
			payload = json.loads(request.content) if request.content else None
			requests.append((request.method, request.url.path, payload))
			if request.url.path == "/collections/myapp-products-v2":
				return httpx.Response(200, json={
					"result": {
						"points_count": 582,
						"config": {"params": {"vectors": {"size": 1024}}},
					},
				})
			if request.url.path == "/aliases":
				return httpx.Response(200, json={
					"result": {"aliases": [{
						"alias_name": "myapp-products-live",
						"collection_name": "myapp-products-v1",
					}]},
				})
			return httpx.Response(200, json={"result": {"status": "ok"}})

		result = ProductVectorClient(
			_settings(), transport=httpx.MockTransport(handler),
		).switch_alias("myapp-products-live", "myapp-products-v2")

		self.assertEqual(result["previous_collection"], "myapp-products-v1")
		self.assertEqual(result["collection"], "myapp-products-v2")
		alias_request = requests[-1]
		self.assertEqual(alias_request[1], "/collections/aliases")
		self.assertEqual(alias_request[2]["actions"], [
			{"delete_alias": {"alias_name": "myapp-products-live"}},
			{"create_alias": {
				"collection_name": "myapp-products-v2",
				"alias_name": "myapp-products-live",
			}},
		])

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


class TestAsyncProductVectorClient(IsolatedAsyncioTestCase):
	async def test_async_search_uses_shared_litellm_and_qdrant_clients(self):
		requests = []

		def litellm_handler(request: httpx.Request):
			requests.append(("litellm", request.url.path, json.loads(request.content)))
			return httpx.Response(200, json={
				"data": [{"index": 0, "embedding": [0.4, 0.5]}],
			})

		def qdrant_handler(request: httpx.Request):
			requests.append(("qdrant", request.url.path, json.loads(request.content)))
			return httpx.Response(200, json={
				"result": [{
					"id": "point-1", "score": 0.91,
					"payload": {
						"item_code": "ITEM-001", "content_hash": "a" * 64,
						"index_version": "product-semantic-v1",
					},
				}],
			})

		litellm = httpx.AsyncClient(
			base_url="http://litellm.test", transport=httpx.MockTransport(litellm_handler),
		)
		qdrant = httpx.AsyncClient(
			base_url="http://qdrant.test", transport=httpx.MockTransport(qdrant_handler),
		)
		client = ProductVectorClient(
			_settings(), litellm_async_client=litellm, qdrant_async_client=qdrant,
		)
		try:
			matches = await client.asearch(ProductVectorSearchRequest(
				query="适合聚会整箱卖的蓝色饮料", item_context="sales", limit=8,
			))
		finally:
			await litellm.aclose()
			await qdrant.aclose()

		self.assertEqual(matches[0].item_code, "ITEM-001")
		self.assertIs(client.litellm_async_client, litellm)
		self.assertIs(client.qdrant_async_client, qdrant)
		self.assertEqual(requests[0][1], "/v1/embeddings")
		self.assertEqual(requests[1][1], "/collections/myapp-products-v1/points/search")
		self.assertIn(
			{"key": "is_sales_item", "match": {"value": 1}},
			requests[1][2]["filter"]["must"],
		)
