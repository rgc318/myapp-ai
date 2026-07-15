from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic

import httpx

from .config import Settings
from .langfuse_client import LangfuseClient


@dataclass(slots=True)
class LangfuseDeliveryMetrics:
	queued_total: int = 0
	sent_total: int = 0
	batch_success_total: int = 0
	batch_failure_total: int = 0
	retry_total: int = 0
	dropped_total: int = 0
	last_error: str | None = None


class LangfuseGenerationDispatcher:
	"""Bounded, fail-open OTLP generation delivery outside the AI request path."""

	def __init__(self, settings: Settings, *, async_client: httpx.AsyncClient | None):
		self.settings = settings
		self.client = LangfuseClient(settings, async_client=async_client)
		self.queue: asyncio.Queue[dict] = asyncio.Queue(
			maxsize=max(1, settings.langfuse_queue_capacity),
		)
		self.metrics = LangfuseDeliveryMetrics()
		self._task: asyncio.Task | None = None
		self._stopping = False

	@property
	def enabled(self) -> bool:
		return self.client.enabled and self.client.async_client is not None

	async def start(self) -> None:
		if not self.enabled or self._task is not None:
			return
		self._stopping = False
		self._task = asyncio.create_task(self._run(), name="langfuse-generation-dispatcher")

	async def stop(self) -> None:
		self._stopping = True
		if self._task is None:
			return
		try:
			await asyncio.wait_for(
				self.queue.join(),
				timeout=max(0.1, self.settings.langfuse_shutdown_timeout_seconds),
			)
		except TimeoutError:
			self._drop_queued("shutdown_timeout")
		try:
			await asyncio.wait_for(
				self._task,
				timeout=max(0.1, self.settings.langfuse_shutdown_timeout_seconds),
			)
		except TimeoutError:
			self._task.cancel()
			try:
				await self._task
			except asyncio.CancelledError:
				pass
		finally:
			self._task = None

	async def arecord_generation(self, **kwargs) -> bool:
		if not self.enabled or self._stopping:
			return False
		payload = self.client.build_generation_otlp_payload(**kwargs)
		try:
			self.queue.put_nowait(payload)
		except asyncio.QueueFull:
			self.metrics.dropped_total += 1
			self.metrics.last_error = "queue_full"
			return False
		self.metrics.queued_total += 1
		return True

	def snapshot(self) -> dict:
		return {
			"enabled": self.enabled,
			"worker_running": bool(self._task and not self._task.done()),
			"queue_depth": self.queue.qsize(),
			"queue_capacity": self.queue.maxsize,
			"queued_total": self.metrics.queued_total,
			"sent_total": self.metrics.sent_total,
			"batch_success_total": self.metrics.batch_success_total,
			"batch_failure_total": self.metrics.batch_failure_total,
			"retry_total": self.metrics.retry_total,
			"dropped_total": self.metrics.dropped_total,
			"last_error": self.metrics.last_error,
		}

	async def _run(self) -> None:
		while not self._stopping or not self.queue.empty():
			try:
				first = await asyncio.wait_for(
					self.queue.get(),
					timeout=max(0.01, self.settings.langfuse_flush_interval_seconds),
				)
			except TimeoutError:
				continue
			batch = [first]
			deadline = monotonic() + max(0.0, self.settings.langfuse_flush_interval_seconds)
			while len(batch) < max(1, self.settings.langfuse_batch_size):
				remaining = deadline - monotonic()
				if remaining <= 0:
					break
				try:
					batch.append(await asyncio.wait_for(self.queue.get(), timeout=remaining))
				except TimeoutError:
					break
			try:
				await self._deliver(batch)
			finally:
				for _payload in batch:
					self.queue.task_done()

	async def _deliver(self, batch: list[dict]) -> None:
		payload = {
			"resourceSpans": [
				resource_span
				for item in batch
				for resource_span in item.get("resourceSpans", [])
			]
		}
		attempts = max(0, self.settings.langfuse_max_retries) + 1
		for attempt in range(attempts):
			if await self.client.apost_otlp_payload(payload):
				self.metrics.sent_total += len(batch)
				self.metrics.batch_success_total += 1
				self.metrics.last_error = None
				return
			if attempt + 1 < attempts:
				self.metrics.retry_total += 1
				await asyncio.sleep(min(0.1 * (2**attempt), 1.0))
		self.metrics.batch_failure_total += 1
		self.metrics.dropped_total += len(batch)
		self.metrics.last_error = "otlp_delivery_failed"

	def _drop_queued(self, reason: str) -> None:
		dropped = 0
		while True:
			try:
				self.queue.get_nowait()
			except asyncio.QueueEmpty:
				break
			self.queue.task_done()
			dropped += 1
		self.metrics.dropped_total += dropped
		if dropped:
			self.metrics.last_error = reason
