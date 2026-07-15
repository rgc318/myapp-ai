from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import threading
import time

import httpx

from .config import Settings
from .schemas import ChatRequest


@dataclass(frozen=True, slots=True)
class ResolvedPolicy:
	policy_code: str | None
	policy_version: int | None
	model_alias: str
	reasoning_effort: str
	max_completion_tokens: int
	timeout_seconds: float
	max_concurrency: int
	requests_per_minute: int
	tokens_per_minute: int
	daily_budget: float
	monthly_budget: float
	budget_currency: str | None
	budget_action: str
	fallback_model_aliases: tuple[str, ...]
	model_costs: dict[str, dict]
	fallback_reason: str | None = None


class RuntimePolicyResolver:
	def __init__(self, transport: httpx.BaseTransport | None = None):
		self.transport = transport
		self._lock = threading.Lock()
		self._policies: list[dict] = []
		self._expires_at = 0.0
		self._has_snapshot = False

	@staticmethod
	def _system_default(settings: Settings, reason: str) -> ResolvedPolicy:
		return ResolvedPolicy(
			policy_code=None,
			policy_version=None,
			model_alias=settings.model,
			reasoning_effort=settings.reasoning_effort,
			max_completion_tokens=settings.max_completion_tokens,
			timeout_seconds=settings.timeout_seconds,
			max_concurrency=0,
			requests_per_minute=0,
			tokens_per_minute=0,
			daily_budget=0,
			monthly_budget=0,
			budget_currency=None,
			budget_action="warn",
			fallback_model_aliases=(),
			model_costs={},
			fallback_reason=reason,
		)

	def _fetch(self, settings: Settings) -> list[dict]:
		with httpx.Client(
			base_url=settings.frappe_base_url,
			timeout=min(settings.timeout_seconds, 10),
			transport=self.transport,
		) as client:
			response = client.get(
				"/api/method/myapp.api.gateway.get_ai_runtime_policy_snapshot_v1",
				headers={
					"Host": settings.frappe_site_host,
					"X-MyApp-AI-Service-Token": settings.service_token,
				},
			)
			response.raise_for_status()
			body = response.json()
		payload = body.get("message", body)
		policies = payload.get("policies") if isinstance(payload, dict) else None
		models = payload.get("models") if isinstance(payload, dict) else None
		if not isinstance(policies, list):
			raise RuntimeError("Frappe returned an invalid AI policy snapshot")
		models = models if isinstance(models, dict) else {}
		result = []
		for item in policies:
			if isinstance(item, dict) and isinstance(item.get("policy"), dict):
				result.append({**item, "_models": models})
		return result

	def _snapshot(self, settings: Settings) -> tuple[list[dict], str | None]:
		now = time.monotonic()
		with self._lock:
			if self._has_snapshot and now < self._expires_at:
				return list(self._policies), None
			try:
				policies = self._fetch(settings)
			except (httpx.HTTPError, RuntimeError, ValueError):
				if self._has_snapshot:
					return list(self._policies), "stale_last_verified_snapshot"
				return [], "policy_service_unavailable"
			self._policies = policies
			self._has_snapshot = True
			self._expires_at = now + max(1.0, min(settings.policy_cache_ttl_seconds, 300.0))
			return list(self._policies), None

	@staticmethod
	def _active_now(policy: dict) -> bool:
		now = datetime.now(timezone.utc)
		for field, is_start in (("effective_from", True), ("effective_to", False)):
			value = policy.get(field)
			if not value:
				continue
			try:
				parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
				if parsed.tzinfo is None:
					parsed = parsed.replace(tzinfo=timezone.utc)
			except ValueError:
				return False
			if is_start and now < parsed:
				return False
			if not is_start and now > parsed:
				return False
		return True

	@staticmethod
	def _rollout_selected(request: ChatRequest, policy: dict) -> bool:
		percentage = float(policy.get("rollout_percentage") or 0)
		if percentage >= 100:
			return True
		if percentage <= 0:
			return False
		seed = str(policy.get("rollout_seed") or "")
		value = f"{request.user}|{request.company or ''}|{request.scenario}|{seed}"
		bucket = int(hashlib.sha256(value.encode("utf-8")).hexdigest()[:8], 16) % 10000
		return bucket < int(percentage * 100)

	@staticmethod
	def _priority(request: ChatRequest, policy: dict) -> int:
		companies = set(policy.get("company_scope") or [])
		roles = set(policy.get("role_scope") or [])
		request_roles = set(request.policy_context.roles if request.policy_context else [])
		if companies:
			if not request.company or request.company not in companies:
				return 0
			if roles:
				return 4 if roles & request_roles else 0
			return 3
		if roles:
			return 0
		return 2

	def resolve(self, settings: Settings, request: ChatRequest) -> ResolvedPolicy:
		policies, snapshot_warning = self._snapshot(settings)
		environment = request.policy_context.environment if request.policy_context else settings.langfuse_environment
		candidates = []
		for item in policies:
			policy = item["policy"]
			if policy.get("scenario") != request.scenario or policy.get("environment") != environment:
				continue
			if not self._active_now(policy) or not self._rollout_selected(request, policy):
				continue
			priority = self._priority(request, policy)
			if priority:
				candidates.append((priority, item))
		if not candidates:
			return self._system_default(settings, snapshot_warning or "no_matching_published_policy")
		max_priority = max(priority for priority, _item in candidates)
		winners = [item for priority, item in candidates if priority == max_priority]
		if len(winners) != 1:
			return self._system_default(settings, "ambiguous_published_policy")
		item = winners[0]
		policy = item["policy"]
		return ResolvedPolicy(
			policy_code=str(item.get("policy_code") or policy.get("policy_code") or "") or None,
			policy_version=int(item.get("policy_version") or 0) or None,
			model_alias=str(policy["primary_model_alias"]),
			reasoning_effort=str(policy.get("reasoning_effort") or settings.reasoning_effort),
			max_completion_tokens=int(policy.get("max_completion_tokens") or settings.max_completion_tokens),
			timeout_seconds=float(policy.get("timeout_seconds") or settings.timeout_seconds),
			max_concurrency=int(policy.get("max_concurrency") or 0),
			requests_per_minute=int(policy.get("requests_per_minute") or 0),
			tokens_per_minute=int(policy.get("tokens_per_minute") or 0),
			daily_budget=float(policy.get("daily_budget") or 0),
			monthly_budget=float(policy.get("monthly_budget") or 0),
			budget_currency=str(policy.get("budget_currency") or "") or None,
			budget_action=str(policy.get("budget_action") or "warn"),
			fallback_model_aliases=tuple(str(value) for value in policy.get("fallback_model_aliases") or []),
			model_costs={
				alias: metadata
				for alias, metadata in (item.get("_models") or {}).items()
				if alias in {policy["primary_model_alias"], *(policy.get("fallback_model_aliases") or [])}
				and isinstance(metadata, dict)
			},
			fallback_reason=snapshot_warning,
		)
