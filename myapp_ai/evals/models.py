from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..schemas import ChatMessage

EvalMode = Literal["offline", "live"]
EvalSeverity = Literal["critical", "normal"]
EvalScenario = Literal[
	"general",
	"intent_parse",
	"product_search",
	"order_query",
	"report_summary",
	"sales_order_draft",
	"purchase_order_draft",
	"inventory_adjustment_draft",
	"product_setup_draft",
]


class StrictModel(BaseModel):
	model_config = ConfigDict(extra="forbid")


class EvalRequest(StrictModel):
	messages: list[ChatMessage] = Field(min_length=1, max_length=20)
	company: str | None = None
	locale: str = "zh-CN"
	context: dict | None = None
	allowed_tools: list[str] | None = None
	requested_prompt_version: str | None = None


class EvalExpected(StrictModel):
	expected_json: dict | None = None
	accepted_json_values: dict[str, list[Any]] = Field(default_factory=dict)
	unordered_json_paths: list[str] = Field(default_factory=list)
	ignored_json_paths: list[str] = Field(default_factory=list)
	allowed_error_codes: list[str] = Field(default_factory=list)
	expected_trajectory: list[dict] = Field(default_factory=list)
	trajectory_match: Literal["exact", "contains", "unordered_contains"] = "exact"
	required_concept_groups: list[list[str]] = Field(default_factory=list)
	forbidden_patterns: list[str] = Field(default_factory=list)
	allowed_identifiers: list[str] | None = None
	expected_tool: str | None = None
	expected_arguments: dict | None = None
	accepted_argument_values: dict[str, list[Any]] = Field(default_factory=dict)
	argument_match: Literal["exact", "contains"] = "exact"
	max_tool_calls: int | None = Field(default=None, ge=0, le=20)
	max_empty_result_retries: int | None = Field(default=None, ge=0, le=10)
	forbidden_tools: list[str] = Field(default_factory=list)


class ReplayResponse(StrictModel):
	status_code: int = Field(default=200, ge=100, le=599)
	model: str = "eval-replay-model"
	content: str | dict | None = None
	body: dict | None = None
	trajectory: list[dict] = Field(default_factory=list)
	usage: dict = Field(
		default_factory=lambda: {
			"prompt_tokens": 20,
			"completion_tokens": 10,
			"total_tokens": 30,
			"completion_tokens_details": {"reasoning_tokens": 0},
		}
	)


class ReplaySpec(StrictModel):
	responses: list[ReplayResponse] = Field(min_length=1)
	tool_results: list[dict] = Field(default_factory=list)


class EvalCase(StrictModel):
	id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]+$")
	dataset_version: str
	scenario: EvalScenario
	severity: EvalSeverity = "normal"
	tags: list[str] = Field(default_factory=list)
	modes: list[EvalMode] = Field(default_factory=lambda: ["offline", "live"])
	request: EvalRequest
	expected: EvalExpected
	replay: ReplaySpec


class ThresholdSet(StrictModel):
	critical_case_pass_rate: float = Field(ge=0, le=1)
	schema_valid_rate: float = Field(ge=0, le=1)
	safety_pass_rate: float = Field(ge=0, le=1)
	forbidden_pattern_pass_rate: float = Field(ge=0, le=1)
	structured_field_accuracy: float = Field(ge=0, le=1)
	normal_case_pass_rate: float = Field(ge=0, le=1)


class ThresholdConfig(StrictModel):
	version: str
	offline: ThresholdSet
	live: ThresholdSet


class GradeResult(StrictModel):
	passed: bool
	metrics: dict[str, float]
	weights: dict[str, float] = Field(default_factory=dict)
	failures: list[str] = Field(default_factory=list)
