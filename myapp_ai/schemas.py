import base64
import binascii
import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ImageAttachment(BaseModel):
	model_config = ConfigDict(extra="forbid")

	attachment_id: str = Field(min_length=1, max_length=140)
	filename: str | None = Field(default=None, max_length=255)
	mime_type: Literal["image/jpeg", "image/png", "image/webp"]
	sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
	width: int | None = Field(default=None, ge=1, le=2400)
	height: int | None = Field(default=None, ge=1, le=2400)
	data_base64: str = Field(min_length=1, max_length=12_000_000)

	@field_validator("data_base64")
	@classmethod
	def validate_image_payload(cls, value: str, info) -> str:
		try:
			content = base64.b64decode(value, validate=True)
		except (binascii.Error, ValueError) as error:
			raise ValueError("Image attachment is not valid base64") from error
		if not content or len(content) > 8 * 1024 * 1024:
			raise ValueError("Image attachment exceeds the 8 MB limit")
		expected = info.data.get("sha256")
		if expected and hashlib.sha256(content).hexdigest() != expected:
			raise ValueError("Image attachment hash mismatch")
		return value


class ChatMessage(BaseModel):
	role: Literal["user", "assistant"]
	content: str = Field(min_length=1, max_length=8000)
	attachments: list[ImageAttachment] = Field(default_factory=list, max_length=4)

	@field_validator("content")
	@classmethod
	def normalize_content(cls, value: str) -> str:
		return value.strip()


class PolicyContext(BaseModel):
	roles: list[str] = Field(default_factory=list, max_length=100)
	environment: Literal["development", "test", "staging", "production"] = "development"


class ChatRequest(BaseModel):
	messages: list[ChatMessage] = Field(min_length=1, max_length=20)
	attachments: list[ImageAttachment] = Field(default_factory=list, max_length=4)
	scenario: Literal[
		"general",
		"intent_parse",
		"product_search",
		"order_query",
		"report_summary",
		"sales_order_draft",
		"purchase_order_draft",
		"inventory_adjustment_draft",
		"product_setup_draft",
	] = "general"
	user: str
	company: str | None = None
	locale: str = "zh-CN"
	context: dict | None = None
	prompt_version: str | None = None
	conversation_id: str | None = None
	run_id: str | None = None
	policy_context: PolicyContext | None = None
	policy_code: str | None = None
	policy_version: int | None = None
	fallback_reason: str | None = None
	model_alias: str | None = Field(
		default=None,
		min_length=1,
		max_length=140,
		pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$",
	)

	def image_attachments(self) -> list[ImageAttachment]:
		result = []
		seen = set()
		for attachment in [
			*(item for message in self.messages for item in message.attachments),
			*self.attachments,
		]:
			if attachment.attachment_id in seen:
				continue
			seen.add(attachment.attachment_id)
			result.append(attachment)
		return result


class ProductSearchAttributes(BaseModel):
	model_config = ConfigDict(extra="forbid")

	brand: str | None = Field(default=None, max_length=140)
	item_group: str | None = Field(default=None, max_length=140)
	color: str | None = Field(default=None, max_length=80)
	flavor: str | None = Field(default=None, max_length=140)
	specification: str | None = Field(default=None, max_length=200)
	capacity: str | None = Field(default=None, max_length=80)
	packaging: str | None = Field(default=None, max_length=140)


class IntentParseCandidate(BaseModel):
	model_config = ConfigDict(extra="forbid")

	intent: Literal[
		"general", "product_search", "order_query", "report_summary",
		"sales_order_draft", "purchase_order_draft", "inventory_adjustment_draft",
		"product_setup_draft",
	]
	confidence: float = Field(ge=0, le=1)
	product_query: str | None = Field(max_length=200)
	product_terms: list[str] = Field(default_factory=list, max_length=8)
	product_hypotheses: list[str] = Field(default_factory=list, max_length=5)
	product_attributes: ProductSearchAttributes = Field(default_factory=ProductSearchAttributes)
	entities: list[Literal[
		"sales_order", "sales_invoice", "purchase_order", "purchase_invoice",
	]] = Field(max_length=4)
	report_type: Literal[
		"overview", "sales", "purchase", "cashflow", "receivable_payable",
	] | None
	date_preset: Literal["all", "today", "this_week", "last_month", "this_month", "last_30_days", "custom"]
	date_from: str | None = Field(max_length=10, pattern=r"^\d{4}-\d{2}-\d{2}$")
	date_to: str | None = Field(max_length=10, pattern=r"^\d{4}-\d{2}-\d{2}$")
	status: Literal["all", "unfinished", "completed", "cancelled", "delivering", "receiving", "paying"]
	sort: Literal["latest", "oldest", "amount_desc", "amount_asc"]
	min_amount: float | None = Field(ge=0, le=1000000000000000)
	limit: int = Field(ge=1, le=20)


class FeedbackRequest(BaseModel):
	trace_id: str
	run_id: str
	rating: Literal["positive", "negative"]
	category: Literal["helpful", "incorrect", "incomplete", "unsafe", "other"] | None = None
	comment: str | None = Field(default=None, max_length=1000)


class ExtractionEvidence(BaseModel):
	model_config = ConfigDict(extra="forbid")

	field: str = Field(min_length=1, max_length=80)
	value: str = Field(min_length=1, max_length=500)
	confidence: float = Field(ge=0, le=1)
	attachment_id: str | None = Field(default=None, max_length=140)


class SalesOrderDraftItem(BaseModel):
	model_config = ConfigDict(extra="forbid")

	item_query: str = Field(min_length=1, max_length=120)
	qty: float | None = Field(default=None, gt=0, le=1000000)
	uom: str | None = Field(default=None, max_length=140)
	price: float | None = Field(default=None, ge=0)
	warehouse_query: str | None = Field(default=None, max_length=140)
	specification_query: str | None = Field(default=None, max_length=300)
	evidence: list[ExtractionEvidence] = Field(default_factory=list, max_length=20)


class SalesOrderDraftCandidate(BaseModel):
	model_config = ConfigDict(extra="forbid")

	operation: Literal["auto", "create", "update"] = "auto"
	order_number: str | None = Field(default=None, max_length=140)
	source_document_type: Literal["unstructured", "our_system_order", "external_order"] = "unstructured"
	customer_query: str | None = Field(default=None, max_length=140)
	transaction_date: str | None = Field(default=None, max_length=20)
	delivery_date: str | None = Field(default=None, max_length=20)
	default_sales_mode: Literal["wholesale", "retail"] | None = None
	warehouse_query: str | None = Field(default=None, max_length=140)
	remarks: str | None = Field(default=None, max_length=1000)
	items: list[SalesOrderDraftItem] = Field(default_factory=list, max_length=50)
	evidence: list[ExtractionEvidence] = Field(default_factory=list, max_length=50)


class PurchaseOrderDraftCandidate(BaseModel):
	model_config = ConfigDict(extra="forbid")

	operation: Literal["auto", "create", "update"] = "auto"
	order_number: str | None = Field(default=None, max_length=140)
	source_document_type: Literal["unstructured", "our_system_order", "external_order"] = "unstructured"
	supplier_query: str | None = Field(default=None, max_length=140)
	transaction_date: str | None = Field(default=None, max_length=20)
	schedule_date: str | None = Field(default=None, max_length=20)
	default_purchase_mode: Literal["wholesale", "retail"] = "wholesale"
	warehouse_query: str | None = Field(default=None, max_length=140)
	currency: str | None = Field(default=None, max_length=20)
	supplier_ref: str | None = Field(default=None, max_length=140)
	remarks: str | None = Field(default=None, max_length=1000)
	items: list[SalesOrderDraftItem] = Field(default_factory=list, max_length=50)
	evidence: list[ExtractionEvidence] = Field(default_factory=list, max_length=50)


class InventoryAdjustmentDraftCandidate(BaseModel):
	model_config = ConfigDict(extra="forbid")

	item_query: str | None = Field(default=None, max_length=120)
	warehouse_query: str | None = Field(default=None, max_length=140)
	adjustment_type: Literal["set_target", "increase", "decrease"] = "set_target"
	quantity: float | None = Field(default=None, ge=0, le=1000000)
	uom: str | None = Field(default=None, max_length=140)
	posting_date: str | None = Field(default=None, max_length=20)
	reason: str | None = Field(default=None, max_length=1000)


class ProductSetupDraftCandidate(BaseModel):
	model_config = ConfigDict(extra="forbid")

	operation: Literal["auto", "create", "update"] = "auto"
	item_name: str | None = Field(default=None, max_length=140)
	item_code: str | None = Field(default=None, max_length=140)
	item_group_query: str | None = Field(default=None, max_length=140)
	brand_query: str | None = Field(default=None, max_length=140)
	stock_uom: str | None = Field(default=None, max_length=140)
	warehouse_query: str | None = Field(default=None, max_length=140)
	opening_qty: float | None = Field(default=None, ge=0, le=1000000000)
	opening_uom: str | None = Field(default=None, max_length=140)
	standard_selling_rate: float | None = Field(default=None, ge=0)
	wholesale_rate: float | None = Field(default=None, ge=0)
	retail_rate: float | None = Field(default=None, ge=0)
	standard_buying_rate: float | None = Field(default=None, ge=0)
	valuation_rate: float | None = Field(default=None, ge=0)
	currency: str | None = Field(default=None, max_length=20)
	description: str | None = Field(default=None, max_length=2000)
	barcode: str | None = Field(default=None, max_length=140)
	specification: str | None = Field(default=None, max_length=500)
	evidence: list[ExtractionEvidence] = Field(default_factory=list, max_length=50)


class TokenUsage(BaseModel):
	prompt_tokens: int = 0
	completion_tokens: int = 0
	total_tokens: int = 0
	reasoning_tokens: int = 0


class IntentParseResponse(BaseModel):
	intent: IntentParseCandidate
	model: str
	model_alias: str
	trace_id: str
	usage: TokenUsage
	warnings: list[str] = Field(default_factory=list)
	policy_code: str | None = None
	policy_version: int | None = None
	fallback_reason: str | None = None
	estimated_cost: float = 0
	cost_currency: str | None = None


class SalesOrderDraftResponse(BaseModel):
	draft: SalesOrderDraftCandidate
	model: str
	model_alias: str
	trace_id: str
	usage: TokenUsage
	warnings: list[str] = Field(default_factory=list)
	policy_code: str | None = None
	policy_version: int | None = None
	fallback_reason: str | None = None
	estimated_cost: float = 0
	cost_currency: str | None = None


class PurchaseOrderDraftResponse(BaseModel):
	draft: PurchaseOrderDraftCandidate
	model: str
	model_alias: str
	trace_id: str
	usage: TokenUsage
	warnings: list[str] = Field(default_factory=list)
	policy_code: str | None = None
	policy_version: int | None = None
	fallback_reason: str | None = None
	estimated_cost: float = 0
	cost_currency: str | None = None


class InventoryAdjustmentDraftResponse(BaseModel):
	draft: InventoryAdjustmentDraftCandidate
	model: str
	model_alias: str
	trace_id: str
	usage: TokenUsage
	warnings: list[str] = Field(default_factory=list)
	policy_code: str | None = None
	policy_version: int | None = None
	fallback_reason: str | None = None
	estimated_cost: float = 0
	cost_currency: str | None = None


class ProductSetupDraftResponse(BaseModel):
	draft: ProductSetupDraftCandidate
	model: str
	model_alias: str
	trace_id: str
	usage: TokenUsage
	warnings: list[str] = Field(default_factory=list)
	policy_code: str | None = None
	policy_version: int | None = None
	fallback_reason: str | None = None
	estimated_cost: float = 0
	cost_currency: str | None = None


class ChatResponse(BaseModel):
	message: ChatMessage
	model: str
	model_alias: str
	trace_id: str
	usage: TokenUsage
	warnings: list[str] = Field(default_factory=list)
	policy_code: str | None = None
	policy_version: int | None = None
	fallback_reason: str | None = None
	estimated_cost: float = 0
	cost_currency: str | None = None


class AgentRequest(ChatRequest):
	run_id: str = Field(min_length=1, max_length=140)
	company: str = Field(min_length=1, max_length=140)
	capability_token: str = Field(min_length=32, max_length=200)
	allowed_tools: list[Literal[
		"search_products", "query_business_documents", "get_business_report",
	]] = Field(min_length=1, max_length=3)
	approval: dict | None = None


class AgentStep(BaseModel):
	step_no: int = Field(ge=1)
	type: Literal["model", "tool", "guardrail"]
	status: Literal["completed", "failed"]
	call_id: str | None = None
	tool: str | None = None
	guardrail_phase: Literal["input", "tool_output", "output"] | None = None
	result_status: str | None = None
	latency_ms: int = Field(default=0, ge=0)
	error_code: str | None = None


class AgentResponse(ChatResponse):
	status: Literal["completed", "waiting_approval"] = "completed"
	agent_steps: list[AgentStep] = Field(default_factory=list)
	tool_calls: list[dict] = Field(default_factory=list)
	tool_results: list[dict] = Field(default_factory=list)
	citations: list[dict] = Field(default_factory=list)
	approval: dict | None = None


class AgentCheckpoint(BaseModel):
	model_config = ConfigDict(extra="forbid")

	schema_version: Literal["agent-state-v1"] = "agent-state-v1"
	run_id: str = Field(min_length=1, max_length=140)
	stage: Literal[
		"input_guardrail", "model_decision", "tool_completed", "waiting_approval", "output_guardrail",
	]
	next_model_step: int = Field(ge=1, le=7)
	tool_count: int = Field(ge=0, le=6)
	runtime_messages: list[dict] = Field(default_factory=list, max_length=40)
	agent_steps: list[AgentStep] = Field(default_factory=list, max_length=40)
	tool_calls: list[dict] = Field(default_factory=list, max_length=6)
	pending_tool_calls: list[dict] = Field(default_factory=list, max_length=3)
	pending_approval: dict | None = None
	tool_results: list[dict] = Field(default_factory=list, max_length=6)
	citations: list[dict] = Field(default_factory=list, max_length=100)
	usage: TokenUsage = Field(default_factory=TokenUsage)
	model: str = Field(default="", max_length=255)
	model_alias: str = Field(default="", max_length=140)
	prompt_version: str = Field(default="", max_length=40)
	trace_id: str = Field(min_length=1, max_length=64)
	agent_span_id: str = Field(min_length=1, max_length=64)
	final_content: str | None = Field(default=None, max_length=8000)


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
	documents: list[ProductVectorDocument] = Field(min_length=1, max_length=128)
	embedding_model: str | None = Field(default=None, min_length=1, max_length=140, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
	collection: str | None = Field(default=None, min_length=1, max_length=140, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ProductVectorDeleteRequest(BaseModel):
	item_codes: list[str] = Field(min_length=1, max_length=100)
	collection: str | None = Field(default=None, min_length=1, max_length=140, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ProductVectorGovernanceStatusRequest(BaseModel):
	collection: str = Field(min_length=1, max_length=140, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
	alias_name: str | None = Field(default=None, min_length=1, max_length=140, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ProductVectorAliasSwitchRequest(BaseModel):
	alias_name: str = Field(min_length=1, max_length=140, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
	target_collection: str = Field(min_length=1, max_length=140, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ProductVectorReleaseValidationRequest(BaseModel):
	release_code: str = Field(min_length=1, max_length=140)
	embedding_model: str = Field(min_length=1, max_length=140)
	collection: str = Field(min_length=1, max_length=140)
	index_version: str = Field(min_length=1, max_length=40)


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


class GovernancePolicyValidationRequest(BaseModel):
	policy: dict


class ModelAvailabilityRequest(BaseModel):
	model_aliases: list[str] = Field(default_factory=list, max_length=100)

	@field_validator("model_aliases")
	@classmethod
	def normalize_model_aliases(cls, values: list[str]) -> list[str]:
		aliases = []
		for value in values:
			alias = str(value or "").strip()
			if not alias or len(alias) > 140:
				raise ValueError("model aliases must be between 1 and 140 characters")
			if alias not in aliases:
				aliases.append(alias)
		return aliases
