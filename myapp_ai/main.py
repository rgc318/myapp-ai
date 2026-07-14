import hmac
import json

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import StreamingResponse
import httpx

from .config import Settings, get_settings
from .langfuse_client import LangfuseClient
from .litellm_client import LiteLLMClient
from .prompts import PromptVersionMismatchError, prompt_versions, with_effective_prompt
from .schemas import (
	ChatRequest,
	ChatResponse,
	FeedbackRequest,
	InventoryAdjustmentDraftResponse,
	PurchaseOrderDraftResponse,
	ProductVectorDeleteRequest,
	ProductVectorSearchRequest,
	ProductVectorSearchResponse,
	ProductVectorUpsertRequest,
	SalesOrderDraftResponse,
)
from .vector_client import ProductVectorClient


app = FastAPI(
	title="myapp AI Orchestrator",
	version="0.1.0",
	docs_url=None,
	redoc_url=None,
	openapi_url=None,
)


def _validated_prompt_request(request, *, scenario: str | None = None):
	try:
		return with_effective_prompt(request, scenario=scenario)
	except PromptVersionMismatchError as error:
		raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


def require_service_token(
	authorization: str | None = Header(default=None),
	settings: Settings = Depends(get_settings),
) -> None:
	expected = f"Bearer {settings.service_token}"
	if not authorization or not hmac.compare_digest(authorization, expected):
		raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid service token")


@app.get("/health")
def health(settings: Settings = Depends(get_settings)):
	return {
		"status": "ok",
		"model_alias": settings.model,
		"litellm_configured": bool(settings.litellm_api_key),
		"langfuse_configured": settings.langfuse_enabled,
		"vector_search_configured": settings.vector_search_enabled,
		"embedding_model": settings.embedding_model or None,
		"vector_collection": settings.qdrant_collection if settings.vector_search_enabled else None,
		"prompt_versions": prompt_versions(),
	}


@app.post(
	"/internal/v1/vector/products/upsert",
	dependencies=[Depends(require_service_token)],
)
def upsert_product_vectors(
	request: ProductVectorUpsertRequest,
	settings: Settings = Depends(get_settings),
):
	try:
		count = ProductVectorClient(settings).upsert(request.documents)
	except httpx.HTTPStatusError as error:
		raise HTTPException(status_code=502, detail="Vector provider rejected the indexing request") from error
	except (httpx.HTTPError, RuntimeError, ValueError) as error:
		raise HTTPException(status_code=503, detail="Product vector indexing is temporarily unavailable") from error
	return {
		"accepted": True,
		"indexed_count": count,
		"embedding_model": settings.embedding_model,
		"collection": settings.qdrant_collection,
	}


@app.post(
	"/internal/v1/vector/products/delete",
	dependencies=[Depends(require_service_token)],
)
def delete_product_vectors(
	request: ProductVectorDeleteRequest,
	settings: Settings = Depends(get_settings),
):
	try:
		count = ProductVectorClient(settings).delete(request.item_codes)
	except httpx.HTTPStatusError as error:
		raise HTTPException(status_code=502, detail="Vector provider rejected the delete request") from error
	except (httpx.HTTPError, RuntimeError, ValueError) as error:
		raise HTTPException(status_code=503, detail="Product vector deletion is temporarily unavailable") from error
	return {"accepted": True, "deleted_count": count, "collection": settings.qdrant_collection}


@app.post(
	"/internal/v1/vector/products/search",
	response_model=ProductVectorSearchResponse,
	dependencies=[Depends(require_service_token)],
)
def search_product_vectors(
	request: ProductVectorSearchRequest,
	settings: Settings = Depends(get_settings),
):
	try:
		matches = ProductVectorClient(settings).search(request)
	except httpx.HTTPStatusError as error:
		raise HTTPException(status_code=502, detail="Vector provider rejected the search request") from error
	except (httpx.HTTPError, RuntimeError, ValueError) as error:
		raise HTTPException(status_code=503, detail="Product vector search is temporarily unavailable") from error
	return ProductVectorSearchResponse(
		matches=matches,
		embedding_model=settings.embedding_model,
		collection=settings.qdrant_collection,
	)


@app.post(
	"/internal/v1/vector/products/status",
	dependencies=[Depends(require_service_token)],
)
def product_vector_status(settings: Settings = Depends(get_settings)):
	try:
		status_payload = ProductVectorClient(settings).status()
	except httpx.HTTPStatusError as error:
		raise HTTPException(status_code=502, detail="Vector provider rejected the status request") from error
	except (httpx.HTTPError, RuntimeError, ValueError) as error:
		raise HTTPException(status_code=503, detail="Product vector status is temporarily unavailable") from error
	return {
		**status_payload,
		"vector_search_configured": settings.vector_search_enabled,
		"embedding_model": settings.embedding_model or None,
	}


@app.post(
	"/internal/v1/feedback",
	dependencies=[Depends(require_service_token)],
)
def feedback(request: FeedbackRequest, settings: Settings = Depends(get_settings)):
	synced = LangfuseClient(settings).record_feedback(
		trace_id=request.trace_id,
		run_id=request.run_id,
		rating=request.rating,
		category=request.category,
		comment=request.comment,
	)
	return {"accepted": True, "observability_synced": synced}


@app.post(
	"/internal/v1/drafts/sales-order",
	response_model=SalesOrderDraftResponse,
	dependencies=[Depends(require_service_token)],
)
def sales_order_draft(request: ChatRequest, settings: Settings = Depends(get_settings)):
	request = _validated_prompt_request(request, scenario="sales_order_draft")
	try:
		return LiteLLMClient(settings).build_sales_order_draft(request)
	except httpx.HTTPStatusError as error:
		raise HTTPException(status_code=502, detail="Model provider rejected the structured draft request") from error
	except (httpx.HTTPError, RuntimeError, ValueError) as error:
		raise HTTPException(status_code=503, detail="AI draft service is temporarily unavailable") from error


@app.post(
	"/internal/v1/drafts/purchase-order",
	response_model=PurchaseOrderDraftResponse,
	dependencies=[Depends(require_service_token)],
)
def purchase_order_draft(request: ChatRequest, settings: Settings = Depends(get_settings)):
	request = _validated_prompt_request(request, scenario="purchase_order_draft")
	try:
		return LiteLLMClient(settings).build_purchase_order_draft(request)
	except httpx.HTTPStatusError as error:
		raise HTTPException(status_code=502, detail="Model provider rejected the structured draft request") from error
	except (httpx.HTTPError, RuntimeError, ValueError) as error:
		raise HTTPException(status_code=503, detail="AI draft service is temporarily unavailable") from error


@app.post(
	"/internal/v1/drafts/inventory-adjustment",
	response_model=InventoryAdjustmentDraftResponse,
	dependencies=[Depends(require_service_token)],
)
def inventory_adjustment_draft(request: ChatRequest, settings: Settings = Depends(get_settings)):
	request = _validated_prompt_request(request, scenario="inventory_adjustment_draft")
	try:
		return LiteLLMClient(settings).build_inventory_adjustment_draft(request)
	except httpx.HTTPStatusError as error:
		raise HTTPException(status_code=502, detail="Model provider rejected the structured draft request") from error
	except (httpx.HTTPError, RuntimeError, ValueError) as error:
		raise HTTPException(status_code=503, detail="AI draft service is temporarily unavailable") from error


@app.post(
	"/internal/v1/chat",
	response_model=ChatResponse,
	dependencies=[Depends(require_service_token)],
)
def chat(request: ChatRequest, settings: Settings = Depends(get_settings)) -> ChatResponse:
	request = _validated_prompt_request(request)
	if len(request.messages) > settings.max_messages:
		raise HTTPException(status_code=422, detail="Too many messages")
	if any(len(message.content) > settings.max_message_chars for message in request.messages):
		raise HTTPException(status_code=422, detail="Message is too long")

	try:
		return LiteLLMClient(settings).chat(request)
	except httpx.HTTPStatusError as error:
		raise HTTPException(status_code=502, detail="Model provider rejected the request") from error
	except (httpx.HTTPError, RuntimeError) as error:
		raise HTTPException(status_code=503, detail="AI service is temporarily unavailable") from error


@app.post(
	"/internal/v1/chat/stream",
	dependencies=[Depends(require_service_token)],
)
def stream_chat(request: ChatRequest, settings: Settings = Depends(get_settings)) -> StreamingResponse:
	request = _validated_prompt_request(request)
	if len(request.messages) > settings.max_messages:
		raise HTTPException(status_code=422, detail="Too many messages")
	if any(len(message.content) > settings.max_message_chars for message in request.messages):
		raise HTTPException(status_code=422, detail="Message is too long")

	def event_stream():
		try:
			for event in LiteLLMClient(settings).stream(request):
				yield f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
		except httpx.HTTPStatusError:
			event = {"type": "error", "code": "MODEL_PROVIDER_REJECTED", "message": "模型供应商拒绝了请求。"}
			yield f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
		except (httpx.HTTPError, RuntimeError, json.JSONDecodeError):
			event = {"type": "error", "code": "AI_SERVICE_UNAVAILABLE", "message": "AI 服务暂时不可用。"}
			yield f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"

	return StreamingResponse(
		event_stream(),
		media_type="text/event-stream",
		headers={
			"Cache-Control": "no-cache, no-transform",
			"Connection": "keep-alive",
			"X-Accel-Buffering": "no",
		},
	)
