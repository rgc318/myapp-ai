import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import httpx

from myapp_ai.config import Settings
from myapp_ai.evals.dataset import EvalConfigurationError, load_dataset, load_thresholds
from myapp_ai.evals.runner import (
	AgentToolReplayHandler,
	InvocationOutcome,
	_agent_request,
	_stable_error_code,
	main,
	run_evaluation,
)


def _settings() -> Settings:
	return Settings(
		litellm_base_url="http://unused.test", litellm_api_key="",
		model="erp-fast-chat", reasoning_effort="none", service_token="service-token",
		timeout_seconds=10, max_messages=20, max_message_chars=8000,
	)


class TestEvalRunner(TestCase):
	def test_provider_errors_use_stable_status_and_transport_codes(self):
		request = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
		response = httpx.Response(403, request=request)

		self.assertEqual(
			_stable_error_code(httpx.HTTPStatusError("rejected", request=request, response=response)),
			"PROVIDER_HTTP_403",
		)
		self.assertEqual(_stable_error_code(httpx.ReadTimeout("slow", request=request)), "PROVIDER_TIMEOUT")

	def test_offline_core_dataset_passes_without_network_or_raw_content(self):
		report = run_evaluation(
			settings=_settings(), mode="offline", dataset=load_dataset("core"),
			thresholds=load_thresholds("thresholds"),
		)

		self.assertTrue(report["summary"]["passed"])
		self.assertEqual(report["schema_version"], "myapp-ai-eval-report-v2")
		self.assertEqual(report["provenance"]["runtime_revision"], "unversioned")
		self.assertEqual(
			set(report["provenance"]["tool_manifest"]["tools"]),
			{"search_products", "query_business_documents", "get_business_report"},
		)
		self.assertEqual(report["dataset"]["case_count"], 37)
		self.assertEqual(report["summary"]["metrics"]["schema_valid_rate"], 1.0)
		self.assertEqual(report["summary"]["metrics"]["safety_pass_rate"], 1.0)
		self.assertEqual(report["summary"]["metrics"]["structured_field_accuracy"], 1.0)
		self.assertEqual(report["summary"]["metrics"]["trajectory_accuracy"], 1.0)
		self.assertEqual(report["summary"]["metrics"]["tool_selection_accuracy"], 1.0)
		self.assertEqual(report["summary"]["gate_scope"], "full")
		self.assertTrue(report["summary"]["release_gate_eligible"])
		self.assertFalse(report["content_included"])
		self.assertTrue(all(
			"output" not in attempt
			for case in report["cases"]
			for attempt in case["attempts"]
		))
		self.assertTrue(all(
			not attempt["observability_synced"]
			for case in report["cases"]
			for attempt in case["attempts"]
		))

	def test_content_is_only_included_when_explicitly_requested(self):
		report = run_evaluation(
			settings=_settings(), mode="offline", dataset=load_dataset("core"),
			thresholds=load_thresholds("thresholds"), include_content=True,
			case_ids={"chat.write_action_refusal"},
		)

		attempt = report["cases"][0]["attempts"][0]
		self.assertIn("output", attempt)
		self.assertIn("不能", attempt["output"])

	def test_offline_runner_returns_all_structured_draft_types(self):
		case_ids = {
			"draft.sales.complete",
			"draft.purchase.complete",
			"draft.inventory.set_target",
			"draft.product_setup.complete",
		}
		report = run_evaluation(
			settings=_settings(), mode="offline", dataset=load_dataset("core"),
			thresholds=load_thresholds("thresholds"), include_content=True,
			case_ids=case_ids,
		)

		self.assertEqual({case["id"] for case in report["cases"]}, case_ids)
		self.assertTrue(all(case["passed"] for case in report["cases"]))
		outputs = {case["scenario"]: case["attempts"][0]["output"] for case in report["cases"]}
		self.assertEqual(outputs["sales_order_draft"]["customer_query"], "华东演示客户")
		self.assertEqual(outputs["purchase_order_draft"]["supplier_query"], "演示供应商甲")
		self.assertEqual(outputs["inventory_adjustment_draft"]["adjustment_type"], "set_target")
		self.assertEqual(outputs["product_setup_draft"]["item_name"], "传承结晶")

	def test_offline_agent_case_executes_runtime_and_reports_actual_trajectory(self):
		report = run_evaluation(
			settings=_settings(), mode="offline", dataset=load_dataset("core"),
			thresholds=load_thresholds("thresholds"), include_content=True,
			case_ids={"agent.product_contains_mo"},
		)

		attempt = report["cases"][0]["attempts"][0]
		self.assertTrue(attempt["passed"])
		self.assertIsNone(attempt["error_code"])
		self.assertIsNone(attempt["error_details"])
		self.assertEqual(attempt["execution_source"], "agent_runtime_replay")
		self.assertEqual(attempt["trajectory"], [{
			"type": "tool", "tool": "search_products",
			"arguments": {
				"query": "莫", "match_mode": "contains",
				"search_fields": ["item_name", "nickname"], "limit": 8,
			},
			"result_status": "resolved",
		}])

	def test_agent_evaluation_exposes_the_full_tool_registry_by_default(self):
		case = next(
			case for case in load_dataset("core").cases
			if case.id == "agent.product_contains_mo"
		)

		request = _agent_request(case)

		self.assertEqual(
			request.allowed_tools,
			["search_products", "query_business_documents", "get_business_report"],
		)

	def test_agent_evaluation_preserves_typed_conversation_context(self):
		case = next(
			case for case in load_dataset("core").cases
			if case.id == "agent.context_exact_order"
		)

		request = _agent_request(case)

		self.assertEqual(request.context, case.request.context)
		self.assertEqual(
			request.context["conversation_state"]["active_entities"]["business_document"]["entity_id"],
			"SO-EVAL-100",
		)

	def test_agent_evaluation_preserves_an_explicit_tool_subset(self):
		source_case = next(
			case for case in load_dataset("core").cases
			if case.id == "agent.product_contains_mo"
		)
		case = source_case.model_copy(update={
			"request": source_case.request.model_copy(
				update={"allowed_tools": ["search_products"]},
			),
		})

		request = _agent_request(case)

		self.assertEqual(request.allowed_tools, ["search_products"])

	def test_agent_tool_replay_matches_results_by_model_selected_tool_order(self):
		handler = AgentToolReplayHandler([
			{"tool": "search_products", "status": "resolved"},
			{"tool": "get_business_report", "status": "resolved"},
		], company="合成演示公司")

		response = handler(httpx.Request(
			"POST",
			"http://frappe.eval/api/method/myapp.api.execute_ai_agent_tool_v1",
			content=json.dumps({
				"tool": "get_business_report",
				"call_id": "call-report-first",
			}).encode(),
		))

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()["message"]["tool"], "get_business_report")
		self.assertEqual(
			response.json()["message"]["grounding"]["result_sets"],
			[{
				"type": "business_report", "complete": True,
				"returned_count": 1, "available_count": 1,
			}],
		)
		self.assertEqual(handler.tool_results, [{"tool": "search_products", "status": "resolved"}])

	def test_agent_expected_trajectory_cannot_replace_actual_runtime_trajectory(self):
		dataset = load_dataset("core")
		case = next(case for case in dataset.cases if case.id == "agent.product_contains_mo")
		case.expected.expected_trajectory = [{
			"type": "tool", "tool": "get_business_report",
			"arguments": {"report_type": "sales"}, "result_status": "resolved",
		}]

		report = run_evaluation(
			settings=_settings(), mode="offline", dataset=dataset,
			thresholds=load_thresholds("thresholds"), include_content=True,
			case_ids={case.id},
		)

		attempt = report["cases"][0]["attempts"][0]
		self.assertEqual([step["tool"] for step in attempt["trajectory"]], ["search_products"])

	def test_agent_empty_result_retry_runs_twice_within_budget(self):
		report = run_evaluation(
			settings=_settings(), mode="offline", dataset=load_dataset("core"),
			thresholds=load_thresholds("thresholds"), include_content=True,
			case_ids={"agent.product_empty_retry_bounded"},
		)

		attempt = report["cases"][0]["attempts"][0]
		self.assertTrue(attempt["passed"])
		self.assertEqual(len(attempt["trajectory"]), 2)
		self.assertEqual(
			[step["result_status"] for step in attempt["trajectory"]],
			["not_found", "not_found"],
		)

	def test_cli_returns_two_when_live_mode_is_not_explicitly_enabled(self):
		with patch.dict(os.environ, {"MYAPP_AI_ENABLE_LIVE_EVALS": "0"}, clear=False):
			self.assertEqual(main(["--mode", "live"]), 2)

	@patch("myapp_ai.evals.runner._invoke_case")
	def test_live_runner_evaluates_every_requested_policy_model(self, invoke_case):
		def outcome(_case, *, settings, mode):
			self.assertEqual(mode, "live")
			return (
				InvocationOutcome(
					output="不能替用户执行写操作。", trace_id=None,
					model=f"provider/{settings.model}", model_alias=settings.model,
					usage={}, latency_ms=1.0, trajectory=[], execution_source="live_provider",
				),
				SimpleNamespace(langfuse=SimpleNamespace()),
			)

		invoke_case.side_effect = outcome
		report = run_evaluation(
			settings=replace(_settings(), litellm_api_key="live-key"),
			mode="live", dataset=load_dataset("core"),
			thresholds=load_thresholds("thresholds"),
			case_ids={"chat.write_action_refusal"},
			model_aliases=["erp-fast-chat", "erp-safe-fallback"],
			sync_langfuse_scores=False,
		)

		attempts = report["cases"][0]["attempts"]
		self.assertEqual(
			[attempt["configured_model_alias"] for attempt in attempts],
			["erp-fast-chat", "erp-safe-fallback"],
		)
		self.assertEqual(report["summary"]["attempt_count"], 2)

	def test_cli_writes_machine_readable_offline_report(self):
		with tempfile.TemporaryDirectory() as directory:
			output = os.path.join(directory, "report.json")
			exit_code = main([
				"--mode", "offline", "--case", "chat.write_action_refusal", "--output", output,
			])
			payload = json.loads(Path(output).read_text(encoding="utf-8"))

		self.assertEqual(exit_code, 0)
		self.assertEqual(payload["schema_version"], "myapp-ai-eval-report-v2")
		self.assertTrue(payload["summary"]["passed"])
		self.assertEqual(payload["summary"]["gate_scope"], "partial")
		self.assertFalse(payload["summary"]["release_gate_eligible"])
		self.assertEqual(payload["summary"]["metrics"]["critical_case_pass_rate"], 1.0)
		self.assertIsNone(payload["summary"]["metrics"]["normal_case_pass_rate"])
		self.assertIsNone(payload["summary"]["metrics"]["structured_field_accuracy"])

	def test_cli_offline_does_not_require_runtime_service_token(self):
		with tempfile.TemporaryDirectory() as directory:
			output = os.path.join(directory, "report.json")
			with patch.dict(os.environ, {}, clear=True):
				exit_code = main([
					"--mode", "offline", "--case", "chat.write_action_refusal", "--output", output,
				])

			self.assertEqual(exit_code, 0)
			self.assertTrue(Path(output).is_file())

	def test_mixed_known_and_unknown_case_ids_are_rejected(self):
		with self.assertRaisesRegex(EvalConfigurationError, "Unknown evaluation case ids"):
			run_evaluation(
				settings=_settings(), mode="offline", dataset=load_dataset("core"),
				thresholds=load_thresholds("thresholds"),
				case_ids={"chat.write_action_refusal", "missing.case"},
			)

	def test_cli_returns_two_for_unknown_case_ids(self):
		self.assertEqual(main([
			"--mode", "offline", "--case", "chat.write_action_refusal",
			"--case", "missing.case",
		]), 2)
