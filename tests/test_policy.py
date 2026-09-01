from dataclasses import replace
from unittest import TestCase

import httpx

from myapp_ai.config import Settings
from myapp_ai.policy import RuntimePolicyResolver
from myapp_ai.schemas import ChatMessage, ChatRequest, PolicyContext


def _settings() -> Settings:
	return Settings(
		litellm_base_url="http://litellm.test",
		litellm_api_key="test-key",
		model="system-default",
		reasoning_effort="none",
		service_token="service-token",
		timeout_seconds=10,
		max_messages=20,
		max_message_chars=8000,
		frappe_base_url="http://frappe.test",
		policy_cache_ttl_seconds=30,
	)


def _request(*, roles=None, company="Demo Company", model_alias=None) -> ChatRequest:
	return ChatRequest(
		messages=[ChatMessage(role="user", content="你好")],
		user="user@example.com",
		company=company,
		scenario="general",
		model_alias=model_alias,
		policy_context=PolicyContext(roles=roles or [], environment="production"),
	)


def _snapshot(*, role_scope=None, company_scope=None, policy_code="general-prod") -> dict:
	return {
		"message": {
			"policies": [{
				"policy_code": policy_code,
				"policy_version": 3,
				"policy": {
					"policy_code": policy_code,
					"scenario": "general",
					"environment": "production",
					"company_scope": company_scope or [],
					"role_scope": role_scope or [],
					"primary_model_alias": "governed-model",
					"fallback_model_aliases": ["governed-fallback"],
					"reasoning_effort": "low",
					"max_completion_tokens": 800,
					"timeout_seconds": 45,
					"max_concurrency": 20,
					"requests_per_minute": 60,
					"tokens_per_minute": 60000,
					"daily_budget": "10",
					"monthly_budget": "200",
					"budget_currency": "CNY",
					"budget_action": "warn",
					"rollout_percentage": "100",
					"rollout_seed": "stable-seed",
				},
			}],
		},
	}


class TestRuntimePolicyResolver(TestCase):
	def test_system_default_includes_configured_fallbacks_and_health_metadata(self):
		settings = replace(
			_settings(),
			fallback_models=("healthy-fallback", "second-fallback"),
		)
		snapshot = _snapshot(company_scope=["Other Company"])
		snapshot["message"]["models"] = {
			"system-default": {"last_health_status": "unavailable"},
			"healthy-fallback": {"last_health_status": "available"},
		}
		resolver = RuntimePolicyResolver(
			httpx.MockTransport(lambda _request: httpx.Response(200, json=snapshot))
		)

		policy = resolver.resolve(settings, _request())

		self.assertEqual(policy.model_alias, "system-default")
		self.assertEqual(
			policy.fallback_model_aliases,
			("healthy-fallback", "second-fallback"),
		)
		self.assertEqual(
			policy.model_costs["system-default"]["last_health_status"],
			"unavailable",
		)
		self.assertEqual(
			policy.model_costs["healthy-fallback"]["last_health_status"],
			"available",
		)

	def test_system_default_includes_explicit_model_health_and_modality_metadata(self):
		snapshot = _snapshot(company_scope=["Other Company"])
		snapshot["message"]["models"] = {
			"system-default": {"supports_vision": False},
			"selected-vision-model": {
				"last_health_status": "available",
				"supports_vision": True,
			},
		}
		resolver = RuntimePolicyResolver(
			httpx.MockTransport(lambda _request: httpx.Response(200, json=snapshot))
		)

		policy = resolver.resolve(
			_settings(), _request(model_alias="selected-vision-model"),
		)

		self.assertTrue(policy.model_costs["selected-vision-model"]["supports_vision"])
		self.assertEqual(
			policy.model_costs["selected-vision-model"]["last_health_status"],
			"available",
		)

	def test_company_and_role_policy_has_highest_priority(self):
		def handler(request: httpx.Request):
			self.assertEqual(request.headers["X-MyApp-AI-Service-Token"], "service-token")
			return httpx.Response(200, json=_snapshot(
				company_scope=["Demo Company"], role_scope=["Sales Manager"],
			))

		policy = RuntimePolicyResolver(httpx.MockTransport(handler)).resolve(
			_settings(), _request(roles=["Sales Manager"]),
		)

		self.assertEqual(policy.policy_code, "general-prod")
		self.assertEqual(policy.policy_version, 3)
		self.assertEqual(policy.model_alias, "governed-model")
		self.assertEqual(policy.max_completion_tokens, 800)
		self.assertIsNone(policy.fallback_reason)

	def test_unmatched_scope_fails_closed_to_system_default(self):
		resolver = RuntimePolicyResolver(httpx.MockTransport(
			lambda _request: httpx.Response(200, json=_snapshot(
				company_scope=["Other Company"], role_scope=["Sales Manager"],
			)),
		))

		policy = resolver.resolve(_settings(), _request(roles=["Sales Manager"]))

		self.assertIsNone(policy.policy_code)
		self.assertEqual(policy.model_alias, "system-default")
		self.assertEqual(policy.fallback_reason, "no_matching_published_policy")

	def test_fetch_failure_uses_last_verified_snapshot(self):
		calls = 0

		def handler(_request: httpx.Request):
			nonlocal calls
			calls += 1
			if calls == 1:
				return httpx.Response(200, json=_snapshot())
			return httpx.Response(503, json={"error": "down"})

		resolver = RuntimePolicyResolver(httpx.MockTransport(handler))
		first = resolver.resolve(_settings(), _request())
		resolver._expires_at = 0
		second = resolver.resolve(_settings(), _request())

		self.assertEqual(first.model_alias, "governed-model")
		self.assertEqual(second.model_alias, "governed-model")
		self.assertEqual(second.fallback_reason, "stale_last_verified_snapshot")

	def test_force_refresh_bypasses_cached_snapshot_for_new_agent_run(self):
		calls = 0

		def handler(_request: httpx.Request):
			nonlocal calls
			calls += 1
			return httpx.Response(200, json=_snapshot(
				policy_code="general-v1" if calls == 1 else "general-v2",
			))

		resolver = RuntimePolicyResolver(httpx.MockTransport(handler))
		first = resolver.resolve(_settings(), _request())
		second = resolver.resolve(_settings(), _request(), force_refresh=True)

		self.assertEqual(first.policy_code, "general-v1")
		self.assertEqual(second.policy_code, "general-v2")
		self.assertEqual(calls, 2)

	def test_invalidate_expires_cached_snapshot_without_discarding_last_verified_data(self):
		calls = 0

		def handler(_request: httpx.Request):
			nonlocal calls
			calls += 1
			return httpx.Response(200, json=_snapshot(
				policy_code="general-v1" if calls == 1 else "general-v2",
			))

		resolver = RuntimePolicyResolver(httpx.MockTransport(handler))
		first = resolver.resolve(_settings(), _request())
		resolver.invalidate()
		second = resolver.resolve(_settings(), _request())

		self.assertEqual(first.policy_code, "general-v1")
		self.assertEqual(second.policy_code, "general-v2")
		self.assertEqual(calls, 2)
