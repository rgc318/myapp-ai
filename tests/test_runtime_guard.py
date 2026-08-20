import hashlib
from unittest import TestCase
from unittest.mock import Mock

from myapp_ai.config import Settings
from myapp_ai.policy import ResolvedPolicy
from myapp_ai.runtime_guard import RuntimeControlUnavailable, RuntimeGuard, RuntimeLimitExceeded
from myapp_ai.schemas import ChatMessage, ChatRequest, ImageAttachment, PolicyContext


def _settings(**overrides) -> Settings:
	values = {
		"litellm_base_url": "http://litellm.test",
		"litellm_api_key": "test-key",
		"model": "system-default",
		"reasoning_effort": "none",
		"service_token": "service-token",
		"timeout_seconds": 10,
		"max_messages": 20,
		"max_message_chars": 8000,
		"redis_url": "redis://redis.test:6379/0",
	}
	values.update(overrides)
	return Settings(**values)


def _policy(**overrides) -> ResolvedPolicy:
	values = {
		"policy_code": "general-prod",
		"policy_version": 2,
		"model_alias": "primary-model",
		"reasoning_effort": "none",
		"max_completion_tokens": 1000,
		"timeout_seconds": 30,
		"max_concurrency": 10,
		"requests_per_minute": 60,
		"tokens_per_minute": 100000,
		"daily_budget": 10,
		"monthly_budget": 200,
		"budget_currency": "CNY",
		"budget_action": "warn",
		"fallback_model_aliases": ("fallback-model",),
		"model_costs": {
			"primary-model": {"input_cost": "2", "output_cost": "8", "currency": "CNY"},
			"fallback-model": {"input_cost": "1", "output_cost": "2", "currency": "CNY"},
		},
		"fallback_reason": None,
	}
	values.update(overrides)
	return ResolvedPolicy(**values)


def _request() -> ChatRequest:
	return ChatRequest(
		messages=[ChatMessage(role="user", content="测试运行时治理")],
		user="user@example.com",
		company="Demo Company",
		scenario="general",
		policy_context=PolicyContext(roles=["Sales User"], environment="production"),
	)


class TestRuntimeGuard(TestCase):
	def test_image_dimensions_are_included_in_predicted_tokens_and_cost(self):
		guard = RuntimeGuard(_settings(redis_url=""), redis_client=None)
		request = _request()
		image_request = request.model_copy(update={
			"attachments": [ImageAttachment(
				attachment_id="AI-ATT-1",
				mime_type="image/webp",
				sha256=hashlib.sha256(b"x").hexdigest(),
				width=2400,
				height=1600,
				data_base64="eA==",
			)],
		})

		text_tokens = guard._predicted_tokens(request, _policy())
		image_tokens = guard._predicted_tokens(image_request, _policy())
		text_cost, _ = guard._predicted_cost(request, _policy(), "primary-model")
		image_cost, _ = guard._predicted_cost(image_request, _policy(), "primary-model")

		self.assertGreater(image_tokens[0], text_tokens[0])
		self.assertGreater(image_cost, text_cost)

	def test_message_level_image_dimensions_are_included_in_predicted_tokens(self):
		guard = RuntimeGuard(_settings(redis_url=""), redis_client=None)
		request = _request()
		message_image_request = request.model_copy(update={
			"messages": [ChatMessage(
				role="user", content="继续分析这张图",
				attachments=[ImageAttachment(
					attachment_id="AI-ATT-HISTORY", mime_type="image/webp",
					sha256=hashlib.sha256(b"x").hexdigest(), width=1200, height=800,
					data_base64="eA==",
				)],
			)],
		})

		self.assertGreater(
			guard._predicted_tokens(message_image_request, _policy())[0],
			guard._predicted_tokens(request, _policy())[0],
		)

	def test_latest_unavailable_health_skips_primary_model(self):
		redis_client = Mock()
		redis_client.get.return_value = None
		redis_client.eval.return_value = [1, "OK", 0]
		guard = RuntimeGuard(_settings(), redis_client=redis_client)
		policy = _policy(model_costs={
			"primary-model": {"last_health_status": "unavailable"},
			"fallback-model": {
				"input_cost": "1",
				"output_cost": "2",
				"currency": "CNY",
				"last_health_status": "available",
			},
		})

		lease = guard.select_and_acquire(policy, _request())

		self.assertEqual(lease.model_alias, "fallback-model")
		self.assertEqual(lease.fallback_reason, "provider_circuit_fallback")
		self.assertEqual(redis_client.eval.call_count, 1)

	def test_governed_limits_fail_closed_without_redis(self):
		guard = RuntimeGuard(_settings(redis_url=""), redis_client=None)

		with self.assertRaises(RuntimeControlUnavailable):
			guard.select_and_acquire(_policy(), _request())

	def test_rate_limit_returns_stable_code_and_retry_after(self):
		redis_client = Mock()
		redis_client.get.return_value = None
		redis_client.eval.return_value = [0, "AI_REQUEST_RATE_LIMITED", 37]
		guard = RuntimeGuard(_settings(), redis_client=redis_client)

		with self.assertRaises(RuntimeLimitExceeded) as caught:
			guard.select_and_acquire(_policy(), _request())

		self.assertEqual(caught.exception.code, "AI_REQUEST_RATE_LIMITED")
		self.assertEqual(caught.exception.retry_after, 37)

	def test_budget_action_selects_lower_cost_fallback(self):
		redis_client = Mock()
		redis_client.get.return_value = None
		redis_client.eval.side_effect = [
			[0, "AI_DAILY_BUDGET_EXCEEDED", 3600],
			[1, "OK", 0],
		]
		guard = RuntimeGuard(_settings(), redis_client=redis_client)

		lease = guard.select_and_acquire(
			_policy(budget_action="use_lower_cost_fallback"),
			_request(),
		)

		self.assertEqual(lease.model_alias, "fallback-model")
		self.assertEqual(lease.fallback_reason, "budget_lower_cost_fallback")
		self.assertLess(lease.predicted_cost, 0.01)

	def test_budget_action_skips_fallback_that_is_not_cheaper(self):
		redis_client = Mock()
		redis_client.get.return_value = None
		redis_client.eval.return_value = [0, "AI_DAILY_BUDGET_EXCEEDED", 3600]
		guard = RuntimeGuard(_settings(), redis_client=redis_client)
		policy = _policy(
			budget_action="use_lower_cost_fallback",
			model_costs={
				"primary-model": {"input_cost": "2", "output_cost": "8", "currency": "CNY"},
				"fallback-model": {"input_cost": "3", "output_cost": "8", "currency": "CNY"},
			},
		)

		with self.assertRaises(RuntimeLimitExceeded) as caught:
			guard.select_and_acquire(policy, _request())

		self.assertEqual(caught.exception.code, "AI_DAILY_BUDGET_EXCEEDED")
		self.assertEqual(redis_client.eval.call_count, 1)

	def test_budget_action_skips_expensive_candidate_and_uses_later_lower_cost_model(self):
		redis_client = Mock()
		redis_client.get.return_value = None
		redis_client.eval.side_effect = [
			[0, "AI_DAILY_BUDGET_EXCEEDED", 3600],
			[1, "OK", 0],
		]
		guard = RuntimeGuard(_settings(), redis_client=redis_client)
		policy = _policy(
			budget_action="use_lower_cost_fallback",
			fallback_model_aliases=("expensive-model", "lower-model"),
			model_costs={
				"primary-model": {"input_cost": "2", "output_cost": "8", "currency": "CNY"},
				"expensive-model": {"input_cost": "3", "output_cost": "9", "currency": "CNY"},
				"lower-model": {"input_cost": "1", "output_cost": "2", "currency": "CNY"},
			},
		)

		lease = guard.select_and_acquire(policy, _request())

		self.assertEqual(lease.model_alias, "lower-model")
		self.assertEqual(lease.fallback_reason, "budget_lower_cost_fallback")
		self.assertEqual(redis_client.eval.call_count, 2)

	def test_enforced_budget_fails_closed_without_registered_model_cost(self):
		redis_client = Mock()
		redis_client.get.return_value = None
		guard = RuntimeGuard(_settings(), redis_client=redis_client)

		with self.assertRaises(RuntimeControlUnavailable):
			guard.select_and_acquire(
				_policy(budget_action="reject_noncritical", model_costs={}),
				_request(),
			)

		redis_client.eval.assert_not_called()

	def test_provider_failure_fallback_does_not_increment_request_rpm_again(self):
		redis_client = Mock()
		redis_client.get.return_value = None
		redis_client.eval.return_value = [1, "OK", 0]
		guard = RuntimeGuard(_settings(), redis_client=redis_client)

		lease = guard.acquire_fallback_after_failure(_policy(), _request(), "primary-model")

		self.assertEqual(lease.model_alias, "fallback-model")
		self.assertEqual(lease.fallback_reason, "provider_error_fallback")
		self.assertEqual(redis_client.eval.call_args.args[-1], 0)

	def test_success_releases_concurrency_and_closes_circuit(self):
		redis_client = Mock()
		redis_client.get.return_value = None
		redis_client.eval.return_value = [1, "OK", 0]
		guard = RuntimeGuard(_settings(), redis_client=redis_client)
		lease = guard.select_and_acquire(_policy(), _request())

		guard.release(
			lease,
			actual_usage={
				"prompt_tokens": 100,
				"completion_tokens": 50,
				"model_cost": _policy().model_costs[lease.model_alias],
			},
			success=True,
		)

		redis_client.zrem.assert_called_once_with(lease.concurrency_key, lease.request_id)
		redis_client.delete.assert_called_once()
