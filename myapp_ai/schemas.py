from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ChatMessage(BaseModel):
	role: Literal["user", "assistant"]
	content: str = Field(min_length=1, max_length=8000)

	@field_validator("content")
	@classmethod
	def normalize_content(cls, value: str) -> str:
		return value.strip()


class ChatRequest(BaseModel):
	messages: list[ChatMessage] = Field(min_length=1, max_length=20)
	scenario: Literal["general", "product_search", "order_query", "report_summary"] = "general"
	user: str
	company: str | None = None
	locale: str = "zh-CN"
	context: dict | None = None
	prompt_version: str = "erp-readonly-v1"


class TokenUsage(BaseModel):
	prompt_tokens: int = 0
	completion_tokens: int = 0
	total_tokens: int = 0
	reasoning_tokens: int = 0


class ChatResponse(BaseModel):
	message: ChatMessage
	model: str
	model_alias: str
	trace_id: str
	usage: TokenUsage
	warnings: list[str] = Field(default_factory=list)
