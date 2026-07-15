from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import asyncio
import re
import uuid

import httpx

from .config import Settings
from .schemas import ProductVectorDocument, ProductVectorMatch, ProductVectorSearchRequest


class ProductVectorClient:
	def __init__(
		self, settings: Settings, transport: httpx.BaseTransport | None = None, *,
		embedding_model: str | None = None, collection: str | None = None,
		litellm_async_client: httpx.AsyncClient | None = None,
		qdrant_async_client: httpx.AsyncClient | None = None,
	):
		self.settings = settings
		self.transport = transport
		resolved_embedding_model = str(embedding_model or settings.embedding_model or "").strip()
		self.embedding_model = (
			self._resource_name(resolved_embedding_model, "embedding model")
			if resolved_embedding_model else ""
		)
		self.collection = self._resource_name(collection or settings.active_qdrant_collection, "collection")
		self.last_embedding_mode = "single"
		self.litellm_async_client = litellm_async_client
		self.qdrant_async_client = qdrant_async_client

	@staticmethod
	def _resource_name(value: str, label: str) -> str:
		resolved = str(value or "").strip()
		if not resolved or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,139}", resolved):
			raise ValueError(f"Invalid governed vector {label} name")
		return resolved

	def _require_configured(self) -> None:
		if not self.settings.litellm_api_key or not self.embedding_model or not self.settings.qdrant_url:
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
			try:
				response = client.post(
					"/v1/embeddings",
					headers=self._embedding_headers(),
					json={"model": self.embedding_model, "input": texts},
				)
				response.raise_for_status()
				rows = sorted(response.json().get("data") or [], key=lambda row: int(row.get("index") or 0))
				vectors = [row.get("embedding") for row in rows]
				self.last_embedding_mode = "batch" if len(texts) > 1 else "single"
			except httpx.HTTPStatusError as error:
				if len(texts) <= 1 or error.response.status_code not in {400, 422, 500}:
					raise

				def embed_one(text: str) -> list[float]:
					individual = client.post(
						"/v1/embeddings",
						headers=self._embedding_headers(),
						json={"model": self.embedding_model, "input": [text]},
					)
					individual.raise_for_status()
					data = individual.json().get("data") or []
					vector = (data[0] if data else {}).get("embedding")
					if not isinstance(vector, list) or not vector:
						raise RuntimeError("Embedding provider returned an invalid individual vector response")
					return vector

				with ThreadPoolExecutor(max_workers=min(8, len(texts))) as executor:
					vectors = list(executor.map(embed_one, texts))
				self.last_embedding_mode = "parallel_single_fallback"
		if len(vectors) != len(texts) or any(not isinstance(vector, list) or not vector for vector in vectors):
			raise RuntimeError("Embedding provider returned an invalid vector response")
		return vectors

	def _collection_path(self, collection: str | None = None) -> str:
		return f"/collections/{self._resource_name(collection or self.collection, 'collection')}"

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
					"collection": self.collection,
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
			"collection": self.collection,
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

	def alias_status(self, alias_name: str) -> dict:
		alias_name = self._resource_name(alias_name, "alias")
		with httpx.Client(
			base_url=self.settings.qdrant_url,
			timeout=self.settings.vector_timeout_seconds,
			transport=self.transport,
		) as client:
			response = client.get("/aliases")
			response.raise_for_status()
		result = response.json().get("result") or {}
		aliases = result.get("aliases") if isinstance(result, dict) else None
		if isinstance(aliases, list):
			match = next(
				(item for item in aliases if str(item.get("alias_name") or "") == alias_name),
				None,
			)
		else:
			match = None
		collection = str((match or {}).get("collection_name") or "").strip() or None
		return {"alias": alias_name, "exists": bool(collection), "collection": collection}

	def switch_alias(self, alias_name: str, target_collection: str) -> dict:
		alias_name = self._resource_name(alias_name, "alias")
		target_collection = self._resource_name(target_collection, "collection")
		if alias_name == target_collection:
			raise ValueError("Vector alias and target collection must be different")
		target = ProductVectorClient(
			self.settings, transport=self.transport,
			embedding_model=self.embedding_model, collection=target_collection,
		).status()
		if not target.get("collection_exists"):
			raise RuntimeError("Target Qdrant collection does not exist")
		current = self.alias_status(alias_name)
		actions = []
		if current.get("exists"):
			actions.append({"delete_alias": {"alias_name": alias_name}})
		actions.append({
			"create_alias": {
				"collection_name": target_collection,
				"alias_name": alias_name,
			}
		})
		with httpx.Client(
			base_url=self.settings.qdrant_url,
			timeout=self.settings.vector_timeout_seconds,
			transport=self.transport,
		) as client:
			response = client.post("/collections/aliases", json={"actions": actions})
			response.raise_for_status()
		return {
			"alias": alias_name,
			"previous_collection": current.get("collection"),
			"collection": target_collection,
			"points_count": int(target.get("points_count") or 0),
			"vector_size": target.get("vector_size"),
		}

	def _require_async_clients(self) -> tuple[httpx.AsyncClient, httpx.AsyncClient]:
		if not self.litellm_async_client or not self.qdrant_async_client:
			raise RuntimeError("Shared vector AsyncClients are not configured")
		return self.litellm_async_client, self.qdrant_async_client

	async def _aembed(self, texts: list[str]) -> list[list[float]]:
		self._require_configured()
		litellm, _qdrant = self._require_async_clients()
		try:
			response = await litellm.post(
				"/v1/embeddings", headers=self._embedding_headers(),
				json={"model": self.embedding_model, "input": texts},
			)
			response.raise_for_status()
			rows = sorted(response.json().get("data") or [], key=lambda row: int(row.get("index") or 0))
			vectors = [row.get("embedding") for row in rows]
			self.last_embedding_mode = "batch" if len(texts) > 1 else "single"
		except httpx.HTTPStatusError as error:
			if len(texts) <= 1 or error.response.status_code not in {400, 422, 500}:
				raise
			semaphore = asyncio.Semaphore(min(8, len(texts)))

			async def embed_one(text: str) -> list[float]:
				async with semaphore:
					individual = await litellm.post(
						"/v1/embeddings", headers=self._embedding_headers(),
						json={"model": self.embedding_model, "input": [text]},
					)
					individual.raise_for_status()
					data = individual.json().get("data") or []
					vector = (data[0] if data else {}).get("embedding")
					if not isinstance(vector, list) or not vector:
						raise RuntimeError("Embedding provider returned an invalid individual vector response")
					return vector

			vectors = await asyncio.gather(*(embed_one(text) for text in texts))
			self.last_embedding_mode = "parallel_single_fallback"
		if len(vectors) != len(texts) or any(not isinstance(vector, list) or not vector for vector in vectors):
			raise RuntimeError("Embedding provider returned an invalid vector response")
		return vectors

	async def _aensure_collection(self, vector_size: int) -> None:
		_litellm, qdrant = self._require_async_clients()
		response = await qdrant.get(self._collection_path())
		if response.status_code == 404:
			created = await qdrant.put(
				self._collection_path(), json={"vectors": {"size": vector_size, "distance": "Cosine"}},
			)
			created.raise_for_status()
			for field_name in ("disabled", "is_sales_item", "is_purchase_item", "is_stock_item"):
				indexed = await qdrant.put(
					f"{self._collection_path()}/index", params={"wait": "true"},
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

	async def aupsert(self, documents: list[ProductVectorDocument]) -> int:
		vectors = await self._aembed([document.text for document in documents])
		await self._aensure_collection(len(vectors[0]))
		points = []
		for document, vector in zip(documents, vectors, strict=True):
			payload = document.model_dump(exclude={"text"})
			points.append({"id": self._point_id(document.item_code), "vector": vector, "payload": payload})
		_litellm, qdrant = self._require_async_clients()
		response = await qdrant.put(
			f"{self._collection_path()}/points", params={"wait": "true"}, json={"points": points},
		)
		response.raise_for_status()
		return len(points)

	async def adelete(self, item_codes: list[str]) -> int:
		self._require_configured()
		_litellm, qdrant = self._require_async_clients()
		response = await qdrant.post(
			f"{self._collection_path()}/points/delete", params={"wait": "true"},
			json={"points": [self._point_id(item_code) for item_code in item_codes]},
		)
		if response.status_code == 404:
			return 0
		response.raise_for_status()
		return len(item_codes)

	async def astatus(self) -> dict:
		if not self.settings.qdrant_url:
			return {"reachable": False, "collection_exists": False}
		_litellm, qdrant = self._require_async_clients()
		response = await qdrant.get(self._collection_path())
		if response.status_code == 404:
			return {
				"reachable": True, "collection_exists": False, "collection": self.collection,
				"points_count": 0, "indexed_vectors_count": 0, "vector_size": None,
			}
		response.raise_for_status()
		result = response.json().get("result") or {}
		vectors = (((result.get("config") or {}).get("params") or {}).get("vectors") or {})
		return {
			"reachable": True, "collection_exists": True, "collection": self.collection,
			"points_count": int(result.get("points_count") or 0),
			"indexed_vectors_count": int(result.get("indexed_vectors_count") or 0),
			"vector_size": int(vectors.get("size")) if vectors.get("size") else None,
		}

	async def asearch(self, request: ProductVectorSearchRequest) -> list[ProductVectorMatch]:
		vector = (await self._aembed([request.query]))[0]
		must = [{"key": "disabled", "match": {"value": 0}}]
		if request.item_context == "sales":
			must.append({"key": "is_sales_item", "match": {"value": 1}})
		elif request.item_context == "purchase":
			must.append({"key": "is_purchase_item", "match": {"value": 1}})
		elif request.item_context == "inventory":
			must.append({"key": "is_stock_item", "match": {"value": 1}})
		_litellm, qdrant = self._require_async_clients()
		response = await qdrant.post(
			f"{self._collection_path()}/points/search",
			json={"vector": vector, "limit": request.limit, "with_payload": True, "filter": {"must": must}},
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
			for row in rows if (row.get("payload") or {}).get("item_code")
		]

	async def aalias_status(self, alias_name: str) -> dict:
		alias_name = self._resource_name(alias_name, "alias")
		_litellm, qdrant = self._require_async_clients()
		response = await qdrant.get("/aliases")
		response.raise_for_status()
		aliases = (response.json().get("result") or {}).get("aliases") or []
		match = next((item for item in aliases if str(item.get("alias_name") or "") == alias_name), None)
		collection = str((match or {}).get("collection_name") or "").strip() or None
		return {"alias": alias_name, "exists": bool(collection), "collection": collection}

	async def aswitch_alias(self, alias_name: str, target_collection: str) -> dict:
		alias_name = self._resource_name(alias_name, "alias")
		target_collection = self._resource_name(target_collection, "collection")
		if alias_name == target_collection:
			raise ValueError("Vector alias and target collection must be different")
		target_client = ProductVectorClient(
			self.settings, embedding_model=self.embedding_model, collection=target_collection,
			litellm_async_client=self.litellm_async_client, qdrant_async_client=self.qdrant_async_client,
		)
		target = await target_client.astatus()
		if not target.get("collection_exists"):
			raise RuntimeError("Target Qdrant collection does not exist")
		current = await self.aalias_status(alias_name)
		actions = []
		if current.get("exists"):
			actions.append({"delete_alias": {"alias_name": alias_name}})
		actions.append({"create_alias": {"collection_name": target_collection, "alias_name": alias_name}})
		_litellm, qdrant = self._require_async_clients()
		response = await qdrant.post("/collections/aliases", json={"actions": actions})
		response.raise_for_status()
		return {
			"alias": alias_name, "previous_collection": current.get("collection"),
			"collection": target_collection, "points_count": int(target.get("points_count") or 0),
			"vector_size": target.get("vector_size"),
		}
