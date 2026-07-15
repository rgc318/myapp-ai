from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import time
import uuid

import redis

from .config import Settings
from .policy import ResolvedPolicy
from .schemas import ChatRequest


ACQUIRE_SCRIPT = """
local now_ms = tonumber(ARGV[1])
local lease_ms = tonumber(ARGV[2])
local request_id = ARGV[3]
local concurrency_limit = tonumber(ARGV[4])
local rpm_limit = tonumber(ARGV[5])
local tpm_limit = tonumber(ARGV[6])
local predicted_tokens = tonumber(ARGV[7])
local predicted_cost = tonumber(ARGV[8])
local daily_budget = tonumber(ARGV[9])
local monthly_budget = tonumber(ARGV[10])
local enforce_budget = tonumber(ARGV[11])
local minute_ttl = tonumber(ARGV[12])
local daily_ttl = tonumber(ARGV[13])
local monthly_ttl = tonumber(ARGV[14])
local daily_retry = tonumber(ARGV[15])
local monthly_retry = tonumber(ARGV[16])
local count_request = tonumber(ARGV[17])

redis.call('ZREMRANGEBYSCORE', KEYS[5], '-inf', now_ms)
if concurrency_limit > 0 and redis.call('ZCARD', KEYS[5]) >= concurrency_limit then
  return {0, 'AI_CONCURRENCY_LIMITED', 1}
end

local rpm_current = tonumber(redis.call('GET', KEYS[1]) or '0')
if count_request == 1 and rpm_limit > 0 and rpm_current + 1 > rpm_limit then
  return {0, 'AI_REQUEST_RATE_LIMITED', minute_ttl}
end

local tpm_current = tonumber(redis.call('GET', KEYS[2]) or '0')
if tpm_limit > 0 and tpm_current + predicted_tokens > tpm_limit then
  return {0, 'AI_TOKEN_RATE_LIMITED', minute_ttl}
end

local daily_current = tonumber(redis.call('GET', KEYS[3]) or '0')
if enforce_budget == 1 and daily_budget > 0 and daily_current + predicted_cost > daily_budget then
  return {0, 'AI_DAILY_BUDGET_EXCEEDED', daily_retry}
end

local monthly_current = tonumber(redis.call('GET', KEYS[4]) or '0')
if enforce_budget == 1 and monthly_budget > 0 and monthly_current + predicted_cost > monthly_budget then
  return {0, 'AI_MONTHLY_BUDGET_EXCEEDED', monthly_retry}
end

if count_request == 1 then
  redis.call('INCR', KEYS[1])
  redis.call('EXPIRE', KEYS[1], minute_ttl)
end
redis.call('INCRBY', KEYS[2], predicted_tokens)
redis.call('EXPIRE', KEYS[2], minute_ttl)
redis.call('INCRBYFLOAT', KEYS[3], predicted_cost)
redis.call('EXPIRE', KEYS[3], daily_ttl)
redis.call('INCRBYFLOAT', KEYS[4], predicted_cost)
redis.call('EXPIRE', KEYS[4], monthly_ttl)
redis.call('ZADD', KEYS[5], now_ms + lease_ms, request_id)
redis.call('EXPIRE', KEYS[5], math.ceil(lease_ms / 1000) + 60)
return {1, 'OK', 0}
"""


class RuntimeLimitExceeded(RuntimeError):
	def __init__(self, code: str, retry_after: int, message: str):
		super().__init__(message)
		self.code = code
		self.retry_after = max(1, int(retry_after or 1))


class RuntimeControlUnavailable(RuntimeError):
	pass


@dataclass(frozen=True, slots=True)
class RuntimeLease:
	request_id: str
	model_alias: str
	concurrency_key: str | None
	daily_budget_key: str | None
	monthly_budget_key: str | None
	predicted_cost: float
	cost_currency: str | None
	fallback_reason: str | None = None


class RuntimeGuard:
	def __init__(self, settings: Settings, redis_client=None):
		self.settings = settings
		self.redis = redis_client
		if self.redis is None and settings.redis_url:
			self.redis = redis.Redis.from_url(
				settings.redis_url,
				decode_responses=True,
				socket_connect_timeout=2,
				socket_timeout=2,
				health_check_interval=30,
			)

	def _key(self, *parts: str) -> str:
		return ":".join([self.settings.redis_key_prefix, *parts])

	@staticmethod
	def _identity(request: ChatRequest) -> str:
		value = f"{request.user}|{request.company or ''}|{request.scenario}"
		return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]

	@staticmethod
	def _predicted_tokens(request: ChatRequest, policy: ResolvedPolicy) -> tuple[int, int]:
		message_chars = sum(len(message.content) for message in request.messages)
		context_chars = len(json.dumps(request.context or {}, ensure_ascii=False, separators=(",", ":")))
		input_tokens = max(1, math.ceil((message_chars + context_chars) / 4))
		return input_tokens, max(1, policy.max_completion_tokens)

	def _predicted_cost(
		self, request: ChatRequest, policy: ResolvedPolicy, model_alias: str, *,
		require_registered: bool = False,
	) -> tuple[float, str | None]:
		metadata = policy.model_costs.get(model_alias) or {}
		if require_registered:
			if not isinstance(metadata, dict) or not {"input_cost", "output_cost"}.issubset(metadata):
				raise RuntimeControlUnavailable(
					f"Model {model_alias} does not have governed cost metadata"
				)
		input_tokens, output_tokens = self._predicted_tokens(request, policy)
		try:
			input_cost = float(metadata.get("input_cost") or 0)
			output_cost = float(metadata.get("output_cost") or 0)
		except (TypeError, ValueError) as error:
			raise RuntimeControlUnavailable(
				f"Model {model_alias} has invalid governed cost metadata"
			) from error
		if input_cost < 0 or output_cost < 0 or (require_registered and input_cost == 0 and output_cost == 0):
			raise RuntimeControlUnavailable(
				f"Model {model_alias} does not have positive governed cost metadata"
			)
		currency = str(metadata.get("currency") or "") or policy.budget_currency
		if require_registered and not str(metadata.get("currency") or "").strip():
			raise RuntimeControlUnavailable(
				f"Model {model_alias} does not have a governed cost currency"
			)
		if policy.budget_currency and currency and currency != policy.budget_currency:
			raise RuntimeControlUnavailable("Model cost currency does not match the governed budget currency")
		return ((input_tokens * input_cost) + (output_tokens * output_cost)) / 1_000_000, currency

	def _requires_redis(self, policy: ResolvedPolicy) -> bool:
		return any((
			policy.max_concurrency,
			policy.requests_per_minute,
			policy.tokens_per_minute,
			policy.daily_budget,
			policy.monthly_budget,
		))

	def _circuit_available(self, model_alias: str) -> bool:
		if not self.redis:
			return True
		open_key = self._key("circuit", model_alias, "open")
		if not self.redis.get(open_key):
			return True
		probe_key = self._key("circuit", model_alias, "half-open-probe")
		return bool(self.redis.set(probe_key, "1", nx=True, ex=15))

	@staticmethod
	def _period_ttls() -> tuple[int, int, int, int]:
		now = datetime.now(timezone.utc)
		next_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
		if now.month == 12:
			next_month = now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
		else:
			next_month = now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
		daily_retry = max(1, int((next_day - now).total_seconds()))
		monthly_retry = max(1, int((next_month - now).total_seconds()))
		return daily_retry + 86400, monthly_retry + 86400, daily_retry, monthly_retry

	def _acquire_model(
		self, policy: ResolvedPolicy, request: ChatRequest, model_alias: str, *,
		enforce_budget: bool, fallback_reason: str | None, count_request: bool = True,
	) -> RuntimeLease:
		if self._requires_redis(policy) and not self.redis:
			raise RuntimeControlUnavailable("Redis is required for governed AI runtime limits")
		if not self._circuit_available(model_alias):
			raise RuntimeLimitExceeded("AI_MODEL_CIRCUIT_OPEN", self.settings.circuit_open_seconds, "Model circuit is open")
		request_id = str(uuid.uuid4())
		predicted_cost, currency = self._predicted_cost(
			request, policy, model_alias,
			require_registered=enforce_budget and bool(policy.daily_budget or policy.monthly_budget),
		)
		if not self.redis:
			return RuntimeLease(request_id, model_alias, None, None, None, predicted_cost, currency, fallback_reason)
		identity = self._identity(request)
		now = datetime.now(timezone.utc)
		minute_bucket = now.strftime("%Y%m%d%H%M")
		daily_bucket = now.strftime("%Y%m%d")
		monthly_bucket = now.strftime("%Y%m")
		rpm_key = self._key("rpm", identity, minute_bucket)
		tpm_key = self._key("tpm", model_alias, minute_bucket)
		daily_key = self._key("budget", policy.policy_code or "system", daily_bucket)
		monthly_key = self._key("budget", policy.policy_code or "system", monthly_bucket)
		concurrency_key = self._key("concurrency", model_alias)
		input_tokens, output_tokens = self._predicted_tokens(request, policy)
		daily_ttl, monthly_ttl, daily_retry, monthly_retry = self._period_ttls()
		try:
			result = self.redis.eval(
				ACQUIRE_SCRIPT,
				5,
				rpm_key, tpm_key, daily_key, monthly_key, concurrency_key,
				int(time.time() * 1000), self.settings.concurrency_lease_seconds * 1000, request_id,
				policy.max_concurrency, policy.requests_per_minute, policy.tokens_per_minute,
				input_tokens + output_tokens, predicted_cost, policy.daily_budget, policy.monthly_budget,
				1 if enforce_budget else 0, 60, daily_ttl, monthly_ttl, daily_retry, monthly_retry,
				1 if count_request else 0,
			)
		except redis.RedisError as error:
			raise RuntimeControlUnavailable("Redis runtime governance is unavailable") from error
		if not result or int(result[0]) != 1:
			code = str(result[1] if result and len(result) > 1 else "AI_RUNTIME_LIMITED")
			retry_after = int(result[2] if result and len(result) > 2 else 1)
			raise RuntimeLimitExceeded(code, retry_after, code.replace("_", " ").title())
		return RuntimeLease(
			request_id, model_alias, concurrency_key, daily_key, monthly_key,
			predicted_cost, currency, fallback_reason,
		)

	def select_and_acquire(self, policy: ResolvedPolicy, request: ChatRequest) -> RuntimeLease:
		aliases = [policy.model_alias, *policy.fallback_model_aliases]
		last_error: RuntimeLimitExceeded | None = None
		budget_reference_cost: float | None = None
		budget_reference_currency: str | None = None
		for index, alias in enumerate(aliases):
			fallback_reason = None if index == 0 else (
				"budget_lower_cost_fallback" if budget_reference_cost is not None
				else "provider_circuit_fallback"
			)
			enforce_budget = policy.budget_action in {"use_lower_cost_fallback", "reject_noncritical"}
			candidate_cost: float | None = None
			candidate_currency: str | None = None
			if enforce_budget and (policy.daily_budget or policy.monthly_budget):
				candidate_cost, candidate_currency = self._predicted_cost(
					request, policy, alias, require_registered=True,
				)
			if budget_reference_cost is not None:
				if candidate_cost is None:
					continue
				if candidate_currency != budget_reference_currency or candidate_cost >= budget_reference_cost:
					continue
			try:
				lease = self._acquire_model(
					policy, request, alias,
					enforce_budget=enforce_budget,
					fallback_reason=fallback_reason,
				)
			except RuntimeLimitExceeded as error:
				last_error = error
				if error.code in {"AI_DAILY_BUDGET_EXCEEDED", "AI_MONTHLY_BUDGET_EXCEEDED"}:
					budget_reference_cost = candidate_cost
					budget_reference_currency = candidate_currency
				can_fallback = index + 1 < len(aliases) and (
					error.code == "AI_MODEL_CIRCUIT_OPEN"
					or (
						error.code in {"AI_DAILY_BUDGET_EXCEEDED", "AI_MONTHLY_BUDGET_EXCEEDED"}
						and policy.budget_action == "use_lower_cost_fallback"
					)
				)
				if can_fallback:
					continue
				raise
			return lease
		if last_error:
			raise last_error
		raise RuntimeControlUnavailable("No governed model is available")

	def acquire_fallback_after_failure(
		self, policy: ResolvedPolicy, request: ChatRequest, failed_model_alias: str,
	) -> RuntimeLease:
		aliases = list(policy.fallback_model_aliases)
		if failed_model_alias in aliases:
			aliases = aliases[aliases.index(failed_model_alias) + 1:]
		last_error: RuntimeLimitExceeded | None = None
		for alias in aliases:
			try:
				return self._acquire_model(
					policy, request, alias,
					enforce_budget=policy.budget_action in {"use_lower_cost_fallback", "reject_noncritical"},
					fallback_reason="provider_error_fallback",
					count_request=False,
				)
			except RuntimeLimitExceeded as error:
				last_error = error
				continue
		if last_error:
			raise last_error
		raise RuntimeControlUnavailable("No validated fallback model is available")

	def release(self, lease: RuntimeLease, *, actual_usage: dict | None = None, success: bool = True, provider_failure: bool = False) -> None:
		if not self.redis:
			return
		try:
			if lease.concurrency_key:
				self.redis.zrem(lease.concurrency_key, lease.request_id)
			if actual_usage and lease.daily_budget_key and lease.monthly_budget_key:
				input_tokens = int(actual_usage.get("prompt_tokens") or 0)
				output_tokens = int(actual_usage.get("completion_tokens") or 0)
				metadata = actual_usage.get("model_cost") or {}
				actual_cost = (
					(input_tokens * float(metadata.get("input_cost") or 0))
					+ (output_tokens * float(metadata.get("output_cost") or 0))
				) / 1_000_000
				delta = actual_cost - lease.predicted_cost
				if delta:
					self.redis.incrbyfloat(lease.daily_budget_key, delta)
					self.redis.incrbyfloat(lease.monthly_budget_key, delta)
			if provider_failure:
				self.record_provider_failure(lease.model_alias)
			elif success:
				self.record_provider_success(lease.model_alias)
		except redis.RedisError:
			return

	def record_provider_failure(self, model_alias: str) -> None:
		if not self.redis:
			return
		failure_key = self._key("circuit", model_alias, "failures")
		pipe = self.redis.pipeline()
		pipe.incr(failure_key)
		pipe.expire(failure_key, self.settings.circuit_failure_window_seconds)
		count = int(pipe.execute()[0])
		if count >= self.settings.circuit_failure_threshold:
			self.redis.set(
				self._key("circuit", model_alias, "open"), "1",
				ex=self.settings.circuit_open_seconds,
			)

	def record_provider_success(self, model_alias: str) -> None:
		if not self.redis:
			return
		self.redis.delete(
			self._key("circuit", model_alias, "failures"),
			self._key("circuit", model_alias, "open"),
			self._key("circuit", model_alias, "half-open-probe"),
		)
