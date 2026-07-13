from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..schemas import ChatMessage


EvalMode = Literal["offline", "live"]
EvalSeverity = Literal["critical", "normal"]
EvalScenario = Literal[
	"general",
	"product_search",
	"order_query",
	"report_summary",
	"sales_order_draft",
	"purchase_order_draft",
	"inventory_adjustment_draft",
]


class StrictModel(BaseModel):
	model_config = ConfigDict(extra="forbid")


class EvalRequest(StrictModel):
	messages: list[ChatMessage] = Field(min_length=1, max_length=20)
	company: str | None = None
	locale: str = "zh-CN"
	context: dict | None = None
	requested_prompt_version: str | None = None


class EvalExpected(StrictModel):
	expected_json: dict | None = None
	required_concept_groups: list[list[str]] = Field(default_factory=list)
	forbidden_patterns: list[str] = Field(default_factory=list)
	allowed_identifiers: list[str] | None = None


class ReplayResponse(StrictModel):
	status_code: int = Field(default=200, ge=100, le=599)
	model: str = "eval-replay-model"
	content: str | dict | None = None
	body: dict | None = None
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
