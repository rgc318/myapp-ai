from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
import asyncio

from fastapi import FastAPI, Request
import httpx

from .config import Settings, get_settings
from .langfuse_dispatcher import LangfuseGenerationDispatcher


@dataclass(slots=True)
class RuntimeHttpClients:
	litellm: httpx.AsyncClient
	qdrant: httpx.AsyncClient
	langfuse: httpx.AsyncClient | None
	langfuse_dispatcher: LangfuseGenerationDispatcher
	chat_semaphore: asyncio.Semaphore
	structured_semaphore: asyncio.Semaphore
	embedding_semaphore: asyncio.Semaphore

	@classmethod
	def create(cls, settings: Settings) -> "RuntimeHttpClients":
		limits = httpx.Limits(
			max_connections=max(1, settings.http_max_connections),
			max_keepalive_connections=max(1, settings.http_max_keepalive_connections),
			keepalive_expiry=max(1.0, settings.http_keepalive_expiry_seconds),
		)
		def timeout(read: float) -> httpx.Timeout:
			return httpx.Timeout(
				connect=max(0.1, settings.http_connect_timeout_seconds),
				read=max(0.1, read),
				write=max(0.1, read),
				pool=max(0.1, settings.http_pool_timeout_seconds),
			)
		litellm = httpx.AsyncClient(
			base_url=settings.litellm_base_url,
			timeout=timeout(settings.timeout_seconds),
			limits=limits,
		)
		qdrant = httpx.AsyncClient(
			base_url=settings.qdrant_url,
			timeout=timeout(settings.vector_timeout_seconds),
			limits=limits,
		)
		langfuse = None
		if settings.langfuse_enabled:
			langfuse = httpx.AsyncClient(
				base_url=settings.langfuse_host,
				timeout=timeout(settings.langfuse_timeout_seconds),
				limits=limits,
				auth=httpx.BasicAuth(
					settings.langfuse_public_key,
					settings.langfuse_secret_key,
				),
			)
		return cls(
			litellm=litellm,
			qdrant=qdrant,
			langfuse=langfuse,
			langfuse_dispatcher=LangfuseGenerationDispatcher(settings, async_client=langfuse),
			chat_semaphore=asyncio.Semaphore(max(1, settings.chat_concurrency)),
			structured_semaphore=asyncio.Semaphore(max(1, settings.structured_concurrency)),
			embedding_semaphore=asyncio.Semaphore(max(1, settings.embedding_concurrency)),
		)

	async def start(self) -> None:
		await self.langfuse_dispatcher.start()

	async def aclose(self) -> None:
		await self.langfuse_dispatcher.stop()
		await self.litellm.aclose()
		await self.qdrant.aclose()
		if self.langfuse:
			await self.langfuse.aclose()


@asynccontextmanager
async def app_lifespan(app: FastAPI):
	clients = RuntimeHttpClients.create(get_settings())
	app.state.http_clients = clients
	await clients.start()
	try:
		yield
	finally:
		await clients.aclose()


def get_runtime_http_clients(request: Request) -> RuntimeHttpClients:
	return request.app.state.http_clients
