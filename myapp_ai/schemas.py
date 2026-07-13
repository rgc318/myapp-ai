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
	scenario: Literal[
		"general",
		"product_search",
		"order_query",
		"report_summary",
		"sales_order_draft",
		"purchase_order_draft",
		"inventory_adjustment_draft",
	] = "general"
	user: str
	company: str | None = None
	locale: str = "zh-CN"
	context: dict | None = None
	prompt_version: str = "erp-readonly-v1"
	conversation_id: str | None = None
	run_id: str | None = None


class FeedbackRequest(BaseModel):
	trace_id: str
	run_id: str
	rating: Literal["positive", "negative"]
	category: Literal["helpful", "incorrect", "incomplete", "unsafe", "other"] | None = None
	comment: str | None = Field(default=None, max_length=1000)


class SalesOrderDraftItem(BaseModel):
	item_query: str = Field(min_length=1, max_length=120)
	qty: float = Field(gt=0, le=1000000)
	uom: str | None = Field(default=None, max_length=140)
	price: float | None = Field(default=None, ge=0)
	warehouse_query: str | None = Field(default=None, max_length=140)


class SalesOrderDraftCandidate(BaseModel):
	customer_query: str | None = Field(default=None, max_length=140)
	transaction_date: str | None = Field(default=None, max_length=20)
	delivery_date: str | None = Field(default=None, max_length=20)
	default_sales_mode: Literal["wholesale", "retail"] = "wholesale"
	warehouse_query: str | None = Field(default=None, max_length=140)
	remarks: str | None = Field(default=None, max_length=1000)
	items: list[SalesOrderDraftItem] = Field(default_factory=list, max_length=50)


class PurchaseOrderDraftCandidate(BaseModel):
	supplier_query: str | None = Field(default=None, max_length=140)
	transaction_date: str | None = Field(default=None, max_length=20)
	schedule_date: str | None = Field(default=None, max_length=20)
	default_purchase_mode: Literal["wholesale", "retail"] = "wholesale"
	warehouse_query: str | None = Field(default=None, max_length=140)
	currency: str | None = Field(default=None, max_length=20)
	supplier_ref: str | None = Field(default=None, max_length=140)
	remarks: str | None = Field(default=None, max_length=1000)
	items: list[SalesOrderDraftItem] = Field(default_factory=list, max_length=50)


class InventoryAdjustmentDraftCandidate(BaseModel):
	item_query: str | None = Field(default=None, max_length=120)
	warehouse_query: str | None = Field(default=None, max_length=140)
	adjustment_type: Literal["set_target", "increase", "decrease"] = "set_target"
	quantity: float | None = Field(default=None, ge=0, le=1000000)
	uom: str | None = Field(default=None, max_length=140)
	posting_date: str | None = Field(default=None, max_length=20)
	reason: str | None = Field(default=None, max_length=1000)


class TokenUsage(BaseModel):
	prompt_tokens: int = 0
	completion_tokens: int = 0
	total_tokens: int = 0
	reasoning_tokens: int = 0


class SalesOrderDraftResponse(BaseModel):
	draft: SalesOrderDraftCandidate
	model: str
	model_alias: str
	trace_id: str
	usage: TokenUsage
	warnings: list[str] = Field(default_factory=list)


class PurchaseOrderDraftResponse(BaseModel):
	draft: PurchaseOrderDraftCandidate
	model: str
	model_alias: str
	trace_id: str
	usage: TokenUsage
	warnings: list[str] = Field(default_factory=list)


class InventoryAdjustmentDraftResponse(BaseModel):
	draft: InventoryAdjustmentDraftCandidate
	model: str
	model_alias: str
	trace_id: str
	usage: TokenUsage
	warnings: list[str] = Field(default_factory=list)


class ChatResponse(BaseModel):
	message: ChatMessage
	model: str
	model_alias: str
	trace_id: str
	usage: TokenUsage
	warnings: list[str] = Field(default_factory=list)
