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
	prompt_version: str | None = None
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


class ProductVectorDocument(BaseModel):
	item_code: str = Field(min_length=1, max_length=140)
	text: str = Field(min_length=1, max_length=8000)
	content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
	index_version: str = Field(min_length=1, max_length=40)
	source_modified: str | None = Field(default=None, max_length=40)
	disabled: int = Field(default=0, ge=0, le=1)
	is_sales_item: int = Field(default=1, ge=0, le=1)
	is_purchase_item: int = Field(default=1, ge=0, le=1)
	is_stock_item: int = Field(default=1, ge=0, le=1)
	item_group: str | None = Field(default=None, max_length=140)
	brand: str | None = Field(default=None, max_length=140)
	company_scope: list[str] = Field(default_factory=lambda: ["*"], max_length=100)


class ProductVectorUpsertRequest(BaseModel):
	documents: list[ProductVectorDocument] = Field(min_length=1, max_length=100)


class ProductVectorDeleteRequest(BaseModel):
	item_codes: list[str] = Field(min_length=1, max_length=100)


class ProductVectorSearchRequest(BaseModel):
	query: str = Field(min_length=1, max_length=500)
	limit: int = Field(default=20, ge=1, le=50)
	item_context: Literal["sales", "purchase", "inventory", "all"] = "sales"
	company: str | None = Field(default=None, max_length=140)


class ProductVectorMatch(BaseModel):
	item_code: str
	score: float
	content_hash: str | None = None
	index_version: str | None = None


class ProductVectorSearchResponse(BaseModel):
	matches: list[ProductVectorMatch] = Field(default_factory=list)
	embedding_model: str
	collection: str
