from __future__ import annotations

import uuid

import httpx

from .config import Settings
from .schemas import ProductVectorDocument, ProductVectorMatch, ProductVectorSearchRequest


class ProductVectorClient:
	def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
		self.settings = settings
		self.transport = transport

	def _require_configured(self) -> None:
		if not self.settings.vector_search_enabled:
			raise RuntimeError("Product vector search is not configured")

	def _embedding_headers(self) -> dict[str, str]:
		return {
			"Authorization": f"Bearer {self.settings.litellm_api_key}",
			"Content-Type": "application/json",
		}

	def _embed(self, texts: list[str]) -> list[list[float]]:
		self._require_configured()
		with httpx.Client(
			base_url=self.settings.litellm_base_url,
			timeout=self.settings.vector_timeout_seconds,
			transport=self.transport,
		) as client:
			response = client.post(
				"/v1/embeddings",
				headers=self._embedding_headers(),
				json={"model": self.settings.embedding_model, "input": texts},
			)
			response.raise_for_status()
			rows = sorted(response.json().get("data") or [], key=lambda row: int(row.get("index") or 0))
		vectors = [row.get("embedding") for row in rows]
		if len(vectors) != len(texts) or any(not isinstance(vector, list) or not vector for vector in vectors):
			raise RuntimeError("Embedding provider returned an invalid vector response")
		return vectors

	def _collection_path(self) -> str:
		return f"/collections/{self.settings.qdrant_collection}"

	def _ensure_collection(self, vector_size: int) -> None:
		with httpx.Client(
			base_url=self.settings.qdrant_url,
			timeout=self.settings.vector_timeout_seconds,
			transport=self.transport,
		) as client:
			response = client.get(self._collection_path())
			if response.status_code == 404:
				created = client.put(
					self._collection_path(),
					json={"vectors": {"size": vector_size, "distance": "Cosine"}},
				)
				created.raise_for_status()
				for field_name in ("disabled", "is_sales_item", "is_purchase_item", "is_stock_item"):
					indexed = client.put(
						f"{self._collection_path()}/index",
						params={"wait": "true"},
						json={"field_name": field_name, "field_schema": "integer"},
					)
					indexed.raise_for_status()
				return
			response.raise_for_status()
			config = (((response.json().get("result") or {}).get("config") or {}).get("params") or {}).get("vectors") or {}
			configured_size = int(config.get("size") or 0)
			if configured_size and configured_size != vector_size:
				raise RuntimeError(
					f"Qdrant collection vector size mismatch: expected {configured_size}, received {vector_size}"
				)

	@staticmethod
	def _point_id(item_code: str) -> str:
		return str(uuid.uuid5(uuid.NAMESPACE_URL, f"myapp-product:{item_code}"))

	def upsert(self, documents: list[ProductVectorDocument]) -> int:
		vectors = self._embed([document.text for document in documents])
		self._ensure_collection(len(vectors[0]))
		points = []
		for document, vector in zip(documents, vectors, strict=True):
			payload = document.model_dump(exclude={"text"})
			points.append({"id": self._point_id(document.item_code), "vector": vector, "payload": payload})
		with httpx.Client(
			base_url=self.settings.qdrant_url,
			timeout=self.settings.vector_timeout_seconds,
			transport=self.transport,
		) as client:
			response = client.put(
				f"{self._collection_path()}/points",
				params={"wait": "true"},
				json={"points": points},
			)
			response.raise_for_status()
		return len(points)

	def delete(self, item_codes: list[str]) -> int:
		self._require_configured()
		with httpx.Client(
			base_url=self.settings.qdrant_url,
			timeout=self.settings.vector_timeout_seconds,
			transport=self.transport,
		) as client:
			response = client.post(
				f"{self._collection_path()}/points/delete",
				params={"wait": "true"},
				json={"points": [self._point_id(item_code) for item_code in item_codes]},
			)
			if response.status_code == 404:
				return 0
			response.raise_for_status()
		return len(item_codes)

	def status(self) -> dict:
		if not self.settings.qdrant_url:
			return {"reachable": False, "collection_exists": False}
		with httpx.Client(
			base_url=self.settings.qdrant_url,
			timeout=self.settings.vector_timeout_seconds,
			transport=self.transport,
		) as client:
			response = client.get(self._collection_path())
			if response.status_code == 404:
				return {
					"reachable": True,
					"collection_exists": False,
					"collection": self.settings.qdrant_collection,
					"points_count": 0,
					"indexed_vectors_count": 0,
					"vector_size": None,
				}
			response.raise_for_status()
		result = response.json().get("result") or {}
		vectors = (((result.get("config") or {}).get("params") or {}).get("vectors") or {})
		return {
			"reachable": True,
			"collection_exists": True,
			"collection": self.settings.qdrant_collection,
			"points_count": int(result.get("points_count") or 0),
			"indexed_vectors_count": int(result.get("indexed_vectors_count") or 0),
			"vector_size": int(vectors.get("size")) if vectors.get("size") else None,
		}

	def search(self, request: ProductVectorSearchRequest) -> list[ProductVectorMatch]:
		vector = self._embed([request.query])[0]
		must = [{"key": "disabled", "match": {"value": 0}}]
		if request.item_context == "sales":
			must.append({"key": "is_sales_item", "match": {"value": 1}})
		elif request.item_context == "purchase":
			must.append({"key": "is_purchase_item", "match": {"value": 1}})
		elif request.item_context == "inventory":
			must.append({"key": "is_stock_item", "match": {"value": 1}})
		with httpx.Client(
			base_url=self.settings.qdrant_url,
			timeout=self.settings.vector_timeout_seconds,
			transport=self.transport,
		) as client:
			response = client.post(
				f"{self._collection_path()}/points/search",
				json={
					"vector": vector,
					"limit": request.limit,
					"with_payload": True,
					"filter": {"must": must},
				},
			)
			if response.status_code == 404:
				return []
			response.raise_for_status()
		rows = response.json().get("result") or []
		return [
			ProductVectorMatch(
				item_code=str((row.get("payload") or {}).get("item_code") or ""),
				score=float(row.get("score") or 0),
				content_hash=(row.get("payload") or {}).get("content_hash"),
				index_version=(row.get("payload") or {}).get("index_version"),
			)
			for row in rows
			if (row.get("payload") or {}).get("item_code")
		]
