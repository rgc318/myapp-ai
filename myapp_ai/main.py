import hmac
import json

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import StreamingResponse
import httpx

from .config import Settings, get_settings
from .langfuse_client import LangfuseClient
from .litellm_client import LiteLLMClient
from .schemas import ChatRequest, ChatResponse, FeedbackRequest, SalesOrderDraftResponse


app = FastAPI(
	title="myapp AI Orchestrator",
	version="0.1.0",
	docs_url=None,
	redoc_url=None,
	openapi_url=None,
)


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
	try:
		return LiteLLMClient(settings).build_sales_order_draft(request)
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
