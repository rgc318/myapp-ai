import asyncio
import hmac
import json
from dataclasses import replace
from functools import lru_cache

import anyio
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from .agent_guardrails import AgentRuntimeError
from .agent_runtime import AgentRuntime
from .agent_tool_client import AgentToolClient
from .config import Settings, get_settings
from .governance import check_model_availability, discover_models, validate_policy, validate_vector_release
from .http_clients import RuntimeHttpClients, app_lifespan, get_runtime_http_clients
from .langfuse_client import LangfuseClient
from .litellm_client import LiteLLMClient
from .policy import ResolvedPolicy, RuntimePolicyResolver
from .prompts import PromptVersionMismatchError, prompt_versions, with_effective_prompt
from .release_provenance import prompt_manifest, tool_manifest
from .runtime_guard import RuntimeControlUnavailable, RuntimeGuard, RuntimeLimitExceeded
from .schemas import (
	AgentRequest,
	AgentResponse,
	ChatRequest,
	ChatResponse,
	FeedbackRequest,
	GovernancePolicyValidationRequest,
	IntentParseResponse,
	InventoryAdjustmentDraftResponse,
	ModelAvailabilityRequest,
	ProductSetupDraftResponse,
	ProductVectorAliasSwitchRequest,
	ProductVectorDeleteRequest,
	ProductVectorGovernanceStatusRequest,
	ProductVectorReleaseValidationRequest,
	ProductVectorSearchRequest,
	ProductVectorSearchResponse,
	ProductVectorUpsertRequest,
	PurchaseOrderDraftResponse,
	SalesOrderDraftResponse,
)
from .vector_client import ProductVectorClient

app = FastAPI(
	title="myapp AI Orchestrator",
	version="0.1.0",
	docs_url=None,
	redoc_url=None,
	openapi_url=None,
	lifespan=app_lifespan,
)

_policy_resolver = RuntimePolicyResolver()


class ModelProviderRejected(RuntimeError):
	"""Provider failure annotated with the governed model selected for the attempt."""

	def __init__(self, *, model_alias: str, provider_status: int | None = None):
		super().__init__("Model provider rejected the governed request")
		self.model_alias = model_alias
		self.provider_status = provider_status


def _provider_error_detail(error: ModelProviderRejected) -> dict:
	detail = {
		"code": "MODEL_PROVIDER_REJECTED",
		"message": "模型供应商拒绝了请求。",
		"model_alias": error.model_alias,
	}
	if error.provider_status:
		detail["provider_error_code"] = f"PROVIDER_HTTP_{error.provider_status}"
	return detail


def _provider_status(error: Exception) -> int | None:
	if isinstance(error, httpx.HTTPStatusError):
		return error.response.status_code
	return None


@lru_cache(maxsize=8)
def _runtime_guard(settings: Settings) -> RuntimeGuard:
	return RuntimeGuard(settings)


def _client_for_policy(
	settings: Settings, policy: ResolvedPolicy, clients: RuntimeHttpClients,
) -> LiteLLMClient:
	effective_settings = replace(
		settings,
		model=policy.model_alias,
		reasoning_effort=policy.reasoning_effort,
		timeout_seconds=policy.timeout_seconds,
		max_completion_tokens=policy.max_completion_tokens,
	)
	return LiteLLMClient(
		effective_settings,
		async_client=clients.litellm,
		langfuse_client=clients.langfuse_dispatcher,
	)


def _model_cost(policy: ResolvedPolicy, model_alias: str) -> dict:
	return policy.model_costs.get(model_alias) or {}


def _actual_cost(policy: ResolvedPolicy, model_alias: str, usage: dict) -> tuple[float, str | None]:
	metadata = _model_cost(policy, model_alias)
	cost = (
		(int(usage.get("prompt_tokens") or 0) * float(metadata.get("input_cost") or 0))
		+ (int(usage.get("completion_tokens") or 0) * float(metadata.get("output_cost") or 0))
	) / 1_000_000
	currency = str(metadata.get("currency") or "") or policy.budget_currency
	return cost, currency


def _with_policy_metadata(response, policy: ResolvedPolicy, *, estimated_cost: float = 0, cost_currency: str | None = None):
	return response.model_copy(update={
		"policy_code": policy.policy_code,
		"policy_version": policy.policy_version,
		"fallback_reason": policy.fallback_reason,
		"estimated_cost": estimated_cost,
		"cost_currency": cost_currency,
	})


def _with_runtime_policy_request(request: ChatRequest, policy: ResolvedPolicy) -> ChatRequest:
	return request.model_copy(update={
		"policy_code": policy.policy_code,
		"policy_version": policy.policy_version,
		"fallback_reason": policy.fallback_reason,
	})


def _with_requested_model(policy: ResolvedPolicy, request: ChatRequest) -> ResolvedPolicy:
	if not request.model_alias:
		return policy
	return replace(
		policy,
		model_alias=request.model_alias,
		fallback_model_aliases=(),
	)


def _with_required_modalities(policy: ResolvedPolicy, request: ChatRequest) -> ResolvedPolicy:
	if not request.attachments:
		return policy
	aliases = [policy.model_alias, *policy.fallback_model_aliases]
	vision_aliases = [
		alias for alias in aliases
		if bool((policy.model_costs.get(alias) or {}).get("supports_vision"))
	]
	if request.model_alias and request.model_alias not in vision_aliases:
		raise HTTPException(
			status_code=422,
			detail={
				"code": "AI_SELECTED_MODEL_NO_VISION",
				"message": "The selected model has not passed image-input validation",
			},
		)
	if not vision_aliases:
		raise HTTPException(
			status_code=503,
			detail={
				"code": "AI_VISION_MODEL_REQUIRED",
				"message": "The selected policy has no validated image-input model",
			},
		)
	return replace(
		policy,
		model_alias=vision_aliases[0],
		fallback_model_aliases=tuple(vision_aliases[1:]),
		fallback_reason=(
			policy.fallback_reason
			if vision_aliases[0] == policy.model_alias
			else "primary_model_lacks_validated_vision"
		),
	)


def _limit_exception(error: RuntimeLimitExceeded) -> HTTPException:
	return HTTPException(
		status_code=status.HTTP_429_TOO_MANY_REQUESTS,
		detail={"code": error.code, "message": str(error)},
		headers={"Retry-After": str(error.retry_after)},
	)


async def _thread_call(function, *args, **kwargs):
	return await anyio.to_thread.run_sync(lambda: function(*args, **kwargs))


async def _acquire_local_slot(semaphore: asyncio.Semaphore, guard, lease):
	try:
		await asyncio.wait_for(semaphore.acquire(), timeout=0.05)
	except TimeoutError as error:
		await _thread_call(
			guard.release, lease,
			actual_usage={"model_cost": {}}, success=False,
		)
		raise HTTPException(
			status_code=status.HTTP_429_TOO_MANY_REQUESTS,
			detail={"code": "AI_LOCAL_CONCURRENCY_LIMITED", "message": "Local AI concurrency pool is full"},
			headers={"Retry-After": "1"},
		) from error


async def _resolve_governed_policy(
	settings: Settings, request: ChatRequest, *, require_tools: bool = False,
	require_policy_handshake: bool = False,
) -> ResolvedPolicy:
	if require_policy_handshake and (
		not request.policy_code or request.policy_version is None or request.policy_version < 1
	):
		raise HTTPException(
			status_code=422,
			detail={
				"code": "AI_AGENT_POLICY_HANDSHAKE_REQUIRED",
				"message": "新 Agent Run 缺少有效的策略版本握手。",
			},
		)
	policy = await _thread_call(_policy_resolver.resolve, settings, request)
	if require_tools and request.policy_code:
		def snapshot_matches_expected(candidate: ResolvedPolicy) -> bool:
			expected_version = int(request.policy_version or 0) or None
			if candidate.policy_code != request.policy_code or candidate.policy_version != expected_version:
				return False
			aliases = {
				candidate.model_alias,
				*candidate.fallback_model_aliases,
				*([request.model_alias] if request.model_alias else []),
			}
			for alias in aliases:
				metadata = candidate.model_costs.get(alias)
				if (
					not isinstance(metadata, dict)
					or metadata.get("status") not in {"active", "validated"}
					or metadata.get("supports_tools") is not True
				):
					return False
			return True

		if not snapshot_matches_expected(policy):
			policy = await _thread_call(
				_policy_resolver.resolve, settings, request, force_refresh=True,
			)
		if not snapshot_matches_expected(policy):
			raise HTTPException(
				status_code=503,
				detail={
					"code": "AI_AGENT_POLICY_SNAPSHOT_MISMATCH",
					"message": "Agent Runtime 策略快照正在更新，请稍后重试。",
				},
			)
	return policy


async def _execute_governed(
	settings: Settings, request: ChatRequest, operation, *,
	clients: RuntimeHttpClients, semaphore: asyncio.Semaphore,
	require_tools: bool = False,
	require_policy_handshake: bool = False,
):
	policy = await _resolve_governed_policy(
		settings,
		request,
		require_tools=require_tools,
		require_policy_handshake=require_policy_handshake,
	)
	policy = _with_requested_model(policy, request)
	policy = _with_required_modalities(policy, request)
	guard = _runtime_guard(settings)
	try:
		lease = await _thread_call(guard.select_and_acquire, policy, request)
	except RuntimeLimitExceeded as error:
		raise _limit_exception(error) from error
	except RuntimeControlUnavailable as error:
		raise HTTPException(status_code=503, detail={"code": "AI_RUNTIME_GOVERNANCE_UNAVAILABLE", "message": str(error)}) from error

	await _acquire_local_slot(semaphore, guard, lease)
	try:
		while True:
			effective_policy = replace(
				policy,
				model_alias=lease.model_alias,
				fallback_reason=lease.fallback_reason or policy.fallback_reason,
			)
			client = _client_for_policy(settings, effective_policy, clients)
			effective_request = _with_runtime_policy_request(request, effective_policy)
			try:
				if require_tools:
					metadata = policy.model_costs.get(lease.model_alias)
					environment = (
						request.policy_context.environment
						if request.policy_context else settings.langfuse_environment
					)
					if metadata and not metadata.get("supports_tools"):
						raise RuntimeError("Selected Agent model has not passed tool-calling validation")
					if not metadata and environment in {"staging", "production"}:
						raise RuntimeError("Selected Agent model lacks verified tool-calling metadata")
				response = await operation(client, effective_request)
			except AgentRuntimeError:
				await _thread_call(
					guard.release,
					lease,
					actual_usage={"model_cost": _model_cost(policy, lease.model_alias)},
					success=False,
				)
				raise
			except (httpx.HTTPError, RuntimeError) as error:
				await _thread_call(
					guard.release,
					lease,
					actual_usage={"model_cost": _model_cost(policy, lease.model_alias)},
					success=False,
					provider_failure=True,
				)
				try:
					lease = await _thread_call(
						guard.acquire_fallback_after_failure, policy, request, lease.model_alias,
					)
				except (RuntimeLimitExceeded, RuntimeControlUnavailable):
					raise ModelProviderRejected(
						model_alias=lease.model_alias,
						provider_status=_provider_status(error),
					) from error
				continue
			except Exception:
				await _thread_call(
					guard.release,
					lease,
					actual_usage={"model_cost": _model_cost(policy, lease.model_alias)},
					success=False,
				)
				raise

			usage = response.usage.model_dump(mode="json")
			await _thread_call(
				guard.release,
				lease,
				actual_usage={**usage, "model_cost": _model_cost(policy, lease.model_alias)},
				success=True,
			)
			cost, currency = _actual_cost(policy, lease.model_alias, usage)
			return _with_policy_metadata(
				response,
				effective_policy,
				estimated_cost=cost,
				cost_currency=currency,
			)
	finally:
		semaphore.release()


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
def health(request: Request, settings: Settings = Depends(get_settings)):
	clients = getattr(request.app.state, "http_clients", None)
	delivery = clients.langfuse_dispatcher.snapshot() if clients else {
		"enabled": settings.langfuse_enabled,
		"worker_running": False,
	}
	return {
		"status": "ok",
		"runtime_revision": settings.runtime_revision,
		"model_alias": settings.model,
		"litellm_configured": bool(settings.litellm_api_key),
		"langfuse_configured": settings.langfuse_enabled,
		"langfuse_delivery": delivery,
		"vector_search_configured": settings.vector_search_enabled,
		"runtime_governance_configured": bool(settings.redis_url),
		"embedding_model": settings.embedding_model or None,
		"vector_collection": settings.active_qdrant_collection if settings.vector_search_enabled else None,
		"prompt_versions": prompt_versions(),
		"prompt_manifest_sha256": prompt_manifest()["sha256"],
		"tool_manifest_sha256": tool_manifest()["sha256"],
	}


@app.get(
	"/internal/v1/governance/models",
	dependencies=[Depends(require_service_token)],
)
def governance_models(settings: Settings = Depends(get_settings)):
	try:
		models = discover_models(settings)
	except httpx.HTTPStatusError as error:
		raise HTTPException(status_code=502, detail="LiteLLM rejected model discovery") from error
	except (httpx.HTTPError, RuntimeError, ValueError) as error:
		raise HTTPException(status_code=503, detail="Model discovery is temporarily unavailable") from error
	return {
		"models": models,
		"source": "litellm",
		"visible_count": sum(1 for model in models if model.get("status") == "active"),
	}


@app.post(
	"/internal/v1/governance/models/availability",
	dependencies=[Depends(require_service_token)],
)
def governance_model_availability(
	request: ModelAvailabilityRequest,
	settings: Settings = Depends(get_settings),
):
	try:
		return check_model_availability(settings, request.model_aliases)
	except httpx.HTTPStatusError as error:
		raise HTTPException(status_code=502, detail="LiteLLM rejected model availability checks") from error
	except (httpx.HTTPError, RuntimeError, ValueError) as error:
		raise HTTPException(status_code=503, detail="Model availability checks are temporarily unavailable") from error


@app.post(
	"/internal/v1/governance/validate-policy",
	dependencies=[Depends(require_service_token)],
)
def governance_validate_policy(
	request: GovernancePolicyValidationRequest,
	settings: Settings = Depends(get_settings),
):
	try:
		return validate_policy(settings, request.policy)
	except httpx.HTTPStatusError as error:
		raise HTTPException(status_code=502, detail="A governance dependency rejected validation") from error
	except (httpx.HTTPError, RuntimeError, ValueError) as error:
		raise HTTPException(status_code=503, detail="Policy validation is temporarily unavailable") from error


@app.post(
	"/internal/v1/vector/products/upsert",
	dependencies=[Depends(require_service_token)],
)
async def upsert_product_vectors(
	request: ProductVectorUpsertRequest,
	settings: Settings = Depends(get_settings),
	clients: RuntimeHttpClients = Depends(get_runtime_http_clients),
):
	client = ProductVectorClient(
		settings,
		embedding_model=request.embedding_model,
		collection=request.collection,
		litellm_async_client=clients.litellm,
		qdrant_async_client=clients.qdrant,
	)
	try:
		await asyncio.wait_for(clients.embedding_semaphore.acquire(), timeout=0.05)
		try:
			count = await client.aupsert(request.documents)
		finally:
			clients.embedding_semaphore.release()
	except TimeoutError as error:
		raise HTTPException(status_code=429, detail={"code": "AI_EMBEDDING_CONCURRENCY_LIMITED"}, headers={"Retry-After": "1"}) from error
	except httpx.HTTPStatusError as error:
		raise HTTPException(status_code=502, detail="Vector provider rejected the indexing request") from error
	except (httpx.HTTPError, RuntimeError, ValueError) as error:
		raise HTTPException(status_code=503, detail="Product vector indexing is temporarily unavailable") from error
	return {
		"accepted": True,
		"indexed_count": count,
		"embedding_model": client.embedding_model,
		"collection": client.collection,
		"embedding_mode": client.last_embedding_mode,
	}


@app.post(
	"/internal/v1/vector/products/delete",
	dependencies=[Depends(require_service_token)],
)
async def delete_product_vectors(
	request: ProductVectorDeleteRequest,
	settings: Settings = Depends(get_settings),
	clients: RuntimeHttpClients = Depends(get_runtime_http_clients),
):
	try:
		client = ProductVectorClient(
			settings, collection=request.collection,
			litellm_async_client=clients.litellm, qdrant_async_client=clients.qdrant,
		)
		count = await client.adelete(request.item_codes)
	except httpx.HTTPStatusError as error:
		raise HTTPException(status_code=502, detail="Vector provider rejected the delete request") from error
	except (httpx.HTTPError, RuntimeError, ValueError) as error:
		raise HTTPException(status_code=503, detail="Product vector deletion is temporarily unavailable") from error
	return {"accepted": True, "deleted_count": count, "collection": client.collection}


@app.post(
	"/internal/v1/vector/products/search",
	response_model=ProductVectorSearchResponse,
	dependencies=[Depends(require_service_token)],
)
async def search_product_vectors(
	request: ProductVectorSearchRequest,
	settings: Settings = Depends(get_settings),
	clients: RuntimeHttpClients = Depends(get_runtime_http_clients),
):
	try:
		await asyncio.wait_for(clients.embedding_semaphore.acquire(), timeout=0.05)
		try:
			matches = await ProductVectorClient(
				settings, litellm_async_client=clients.litellm, qdrant_async_client=clients.qdrant,
			).asearch(request)
		finally:
			clients.embedding_semaphore.release()
	except TimeoutError as error:
		raise HTTPException(status_code=429, detail={"code": "AI_EMBEDDING_CONCURRENCY_LIMITED"}, headers={"Retry-After": "1"}) from error
	except httpx.HTTPStatusError as error:
		raise HTTPException(status_code=502, detail="Vector provider rejected the search request") from error
	except (httpx.HTTPError, RuntimeError, ValueError) as error:
		raise HTTPException(status_code=503, detail="Product vector search is temporarily unavailable") from error
	return ProductVectorSearchResponse(
		matches=matches,
		embedding_model=settings.embedding_model,
		collection=settings.active_qdrant_collection,
	)


@app.post(
	"/internal/v1/vector/products/status",
	dependencies=[Depends(require_service_token)],
)
async def product_vector_status(
	settings: Settings = Depends(get_settings),
	clients: RuntimeHttpClients = Depends(get_runtime_http_clients),
):
	try:
		status_payload = await ProductVectorClient(
			settings, litellm_async_client=clients.litellm, qdrant_async_client=clients.qdrant,
		).astatus()
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
	"/internal/v1/vector/governance/status",
	dependencies=[Depends(require_service_token)],
)
async def product_vector_governance_status(
	request: ProductVectorGovernanceStatusRequest,
	settings: Settings = Depends(get_settings),
	clients: RuntimeHttpClients = Depends(get_runtime_http_clients),
):
	try:
		client = ProductVectorClient(
			settings, collection=request.collection,
			litellm_async_client=clients.litellm, qdrant_async_client=clients.qdrant,
		)
		result = await client.astatus()
		alias = await client.aalias_status(request.alias_name) if request.alias_name else None
	except httpx.HTTPStatusError as error:
		raise HTTPException(status_code=502, detail="Vector provider rejected the governance status request") from error
	except (httpx.HTTPError, RuntimeError, ValueError) as error:
		raise HTTPException(status_code=503, detail="Vector governance status is temporarily unavailable") from error
	return {**result, "alias": alias}


@app.post(
	"/internal/v1/vector/governance/switch-alias",
	dependencies=[Depends(require_service_token)],
)
async def switch_product_vector_alias(
	request: ProductVectorAliasSwitchRequest,
	settings: Settings = Depends(get_settings),
	clients: RuntimeHttpClients = Depends(get_runtime_http_clients),
):
	try:
		return await ProductVectorClient(
			settings, litellm_async_client=clients.litellm, qdrant_async_client=clients.qdrant,
		).aswitch_alias(
			request.alias_name, request.target_collection,
		)
	except httpx.HTTPStatusError as error:
		raise HTTPException(status_code=502, detail="Vector provider rejected the alias switch") from error
	except (httpx.HTTPError, RuntimeError, ValueError) as error:
		raise HTTPException(status_code=503, detail="Vector alias switch is temporarily unavailable") from error


@app.post(
	"/internal/v1/vector/governance/validate-release",
	dependencies=[Depends(require_service_token)],
)
def validate_product_vector_release(
	request: ProductVectorReleaseValidationRequest,
	settings: Settings = Depends(get_settings),
):
	return validate_vector_release(settings, request.model_dump())


@app.post(
	"/internal/v1/feedback",
	dependencies=[Depends(require_service_token)],
)
async def feedback(
	request: FeedbackRequest,
	settings: Settings = Depends(get_settings),
	clients: RuntimeHttpClients = Depends(get_runtime_http_clients),
):
	synced = await LangfuseClient(settings, async_client=clients.langfuse).arecord_feedback(
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
async def sales_order_draft(
	request: ChatRequest,
	settings: Settings = Depends(get_settings),
	clients: RuntimeHttpClients = Depends(get_runtime_http_clients),
):
	request = _validated_prompt_request(request, scenario="sales_order_draft")
	try:
		return await _execute_governed(
			settings, request, lambda client, effective: client.abuild_sales_order_draft(effective),
			clients=clients, semaphore=clients.structured_semaphore,
		)
	except ModelProviderRejected as error:
		raise HTTPException(status_code=502, detail=_provider_error_detail(error)) from error
	except httpx.HTTPStatusError as error:
		raise HTTPException(
			status_code=502,
			detail=_provider_error_detail(ModelProviderRejected(
				model_alias=request.model_alias or settings.model,
				provider_status=error.response.status_code,
			)),
		) from error
	except (httpx.HTTPError, RuntimeError, ValueError) as error:
		raise HTTPException(status_code=503, detail="AI draft service is temporarily unavailable") from error


@app.post(
	"/internal/v1/intent/parse",
	response_model=IntentParseResponse,
	dependencies=[Depends(require_service_token)],
)
async def parse_intent(
	request: ChatRequest,
	settings: Settings = Depends(get_settings),
	clients: RuntimeHttpClients = Depends(get_runtime_http_clients),
):
	request = _validated_prompt_request(request, scenario="intent_parse")
	try:
		return await _execute_governed(
			settings, request, lambda client, effective: client.aparse_intent(effective),
			clients=clients, semaphore=clients.structured_semaphore,
		)
	except ModelProviderRejected as error:
		raise HTTPException(status_code=502, detail=_provider_error_detail(error)) from error
	except httpx.HTTPStatusError as error:
		raise HTTPException(
			status_code=502,
			detail=_provider_error_detail(ModelProviderRejected(
				model_alias=request.model_alias or settings.model,
				provider_status=error.response.status_code,
			)),
		) from error
	except (httpx.HTTPError, RuntimeError, ValueError) as error:
		raise HTTPException(status_code=503, detail="AI intent parsing is temporarily unavailable") from error


@app.post(
	"/internal/v1/drafts/purchase-order",
	response_model=PurchaseOrderDraftResponse,
	dependencies=[Depends(require_service_token)],
)
async def purchase_order_draft(
	request: ChatRequest,
	settings: Settings = Depends(get_settings),
	clients: RuntimeHttpClients = Depends(get_runtime_http_clients),
):
	request = _validated_prompt_request(request, scenario="purchase_order_draft")
	try:
		return await _execute_governed(
			settings, request, lambda client, effective: client.abuild_purchase_order_draft(effective),
			clients=clients, semaphore=clients.structured_semaphore,
		)
	except ModelProviderRejected as error:
		raise HTTPException(status_code=502, detail=_provider_error_detail(error)) from error
	except httpx.HTTPStatusError as error:
		raise HTTPException(
			status_code=502,
			detail=_provider_error_detail(ModelProviderRejected(
				model_alias=request.model_alias or settings.model,
				provider_status=error.response.status_code,
			)),
		) from error
	except (httpx.HTTPError, RuntimeError, ValueError) as error:
		raise HTTPException(status_code=503, detail="AI draft service is temporarily unavailable") from error


@app.post(
	"/internal/v1/drafts/inventory-adjustment",
	response_model=InventoryAdjustmentDraftResponse,
	dependencies=[Depends(require_service_token)],
)
async def inventory_adjustment_draft(
	request: ChatRequest,
	settings: Settings = Depends(get_settings),
	clients: RuntimeHttpClients = Depends(get_runtime_http_clients),
):
	request = _validated_prompt_request(request, scenario="inventory_adjustment_draft")
	try:
		return await _execute_governed(
			settings, request, lambda client, effective: client.abuild_inventory_adjustment_draft(effective),
			clients=clients, semaphore=clients.structured_semaphore,
		)
	except ModelProviderRejected as error:
		raise HTTPException(status_code=502, detail=_provider_error_detail(error)) from error
	except httpx.HTTPStatusError as error:
		raise HTTPException(
			status_code=502,
			detail=_provider_error_detail(ModelProviderRejected(
				model_alias=request.model_alias or settings.model,
				provider_status=error.response.status_code,
			)),
		) from error
	except (httpx.HTTPError, RuntimeError, ValueError) as error:
		raise HTTPException(status_code=503, detail="AI draft service is temporarily unavailable") from error


@app.post(
	"/internal/v1/drafts/product-setup",
	response_model=ProductSetupDraftResponse,
	dependencies=[Depends(require_service_token)],
)
async def product_setup_draft(
	request: ChatRequest,
	settings: Settings = Depends(get_settings),
	clients: RuntimeHttpClients = Depends(get_runtime_http_clients),
):
	request = _validated_prompt_request(request, scenario="product_setup_draft")
	try:
		return await _execute_governed(
			settings, request, lambda client, effective: client.abuild_product_setup_draft(effective),
			clients=clients, semaphore=clients.structured_semaphore,
		)
	except ModelProviderRejected as error:
		raise HTTPException(status_code=502, detail=_provider_error_detail(error)) from error
	except httpx.HTTPStatusError as error:
		raise HTTPException(
			status_code=502,
			detail=_provider_error_detail(ModelProviderRejected(
				model_alias=request.model_alias or settings.model,
				provider_status=error.response.status_code,
			)),
		) from error
	except (httpx.HTTPError, RuntimeError, ValueError) as error:
		raise HTTPException(status_code=503, detail="AI draft service is temporarily unavailable") from error


@app.post(
	"/internal/v1/chat",
	response_model=ChatResponse,
	dependencies=[Depends(require_service_token)],
)
async def chat(
	request: ChatRequest,
	settings: Settings = Depends(get_settings),
	clients: RuntimeHttpClients = Depends(get_runtime_http_clients),
) -> ChatResponse:
	request = _validated_prompt_request(request)
	if len(request.messages) > settings.max_messages:
		raise HTTPException(status_code=422, detail="Too many messages")
	if any(len(message.content) > settings.max_message_chars for message in request.messages):
		raise HTTPException(status_code=422, detail="Message is too long")

	try:
		return await _execute_governed(
			settings, request, lambda client, effective: client.achat(effective),
			clients=clients, semaphore=clients.chat_semaphore,
		)
	except ModelProviderRejected as error:
		raise HTTPException(status_code=502, detail=_provider_error_detail(error)) from error
	except httpx.HTTPStatusError as error:
		raise HTTPException(
			status_code=502,
			detail=_provider_error_detail(ModelProviderRejected(
				model_alias=request.model_alias or settings.model,
				provider_status=error.response.status_code,
			)),
		) from error
	except (httpx.HTTPError, RuntimeError) as error:
		raise HTTPException(status_code=503, detail="AI service is temporarily unavailable") from error


@app.post(
	"/internal/v1/agent/run",
	response_model=AgentResponse,
	dependencies=[Depends(require_service_token)],
)
async def run_agent(
	request: AgentRequest,
	settings: Settings = Depends(get_settings),
	clients: RuntimeHttpClients = Depends(get_runtime_http_clients),
) -> AgentResponse:
	request = _validated_prompt_request(request)
	try:
		return await _execute_governed(
			settings,
			request,
			lambda client, effective: AgentRuntime(
				client,
				AgentToolClient(settings, async_client=clients.frappe),
			).run(effective),
			clients=clients,
			semaphore=clients.chat_semaphore,
			require_tools=True,
			require_policy_handshake=True,
		)
	except AgentRuntimeError as error:
		status_code = 504 if error.code == "AI_AGENT_DEADLINE_EXCEEDED" else 409 if error.code == "AI_RUN_CANCELLED" else 422
		raise HTTPException(
			status_code=status_code,
			detail={"code": error.code, "message": str(error), "retryable": error.retryable},
		) from error
	except httpx.HTTPStatusError as error:
		raise HTTPException(status_code=502, detail="Agent model or tool provider rejected the request") from error
	except (httpx.HTTPError, RuntimeError, ValueError) as error:
		raise HTTPException(status_code=503, detail="AI Agent service is temporarily unavailable") from error


@app.post(
	"/internal/v1/agent/run/resume",
	response_model=AgentResponse,
	dependencies=[Depends(require_service_token)],
)
async def resume_agent(
	request: AgentRequest,
	settings: Settings = Depends(get_settings),
	clients: RuntimeHttpClients = Depends(get_runtime_http_clients),
) -> AgentResponse:
	request = _validated_prompt_request(request)
	try:
		return await _execute_governed(
			settings,
			request,
			lambda client, effective: AgentRuntime(
				client,
				AgentToolClient(settings, async_client=clients.frappe),
			).resume(effective),
			clients=clients,
			semaphore=clients.chat_semaphore,
			require_tools=True,
		)
	except AgentRuntimeError as error:
		status_code = 504 if error.code == "AI_AGENT_DEADLINE_EXCEEDED" else 409 if error.code == "AI_RUN_CANCELLED" else 422
		raise HTTPException(
			status_code=status_code,
			detail={"code": error.code, "message": str(error), "retryable": error.retryable},
		) from error
	except httpx.HTTPStatusError as error:
		raise HTTPException(status_code=502, detail="Agent model or tool provider rejected the request") from error
	except (httpx.HTTPError, RuntimeError, ValueError) as error:
		raise HTTPException(status_code=503, detail="AI Agent service is temporarily unavailable") from error


@app.post(
	"/internal/v1/agent/run/stream",
	dependencies=[Depends(require_service_token)],
)
async def stream_agent(
	request: AgentRequest,
	settings: Settings = Depends(get_settings),
	clients: RuntimeHttpClients = Depends(get_runtime_http_clients),
	resume: bool = False,
) -> StreamingResponse:
	request = _validated_prompt_request(request)
	try:
		policy = await _resolve_governed_policy(
			settings,
			request,
			require_tools=True,
			require_policy_handshake=not resume,
		)
	except RuntimeError as error:
		raise HTTPException(status_code=503, detail={"code": "AI_RUNTIME_POLICY_UNAVAILABLE", "message": str(error)}) from error
	policy = _with_requested_model(policy, request)
	policy = _with_required_modalities(policy, request)
	guard = _runtime_guard(settings)
	try:
		lease = await _thread_call(guard.select_and_acquire, policy, request)
	except RuntimeLimitExceeded as error:
		raise _limit_exception(error) from error
	except RuntimeControlUnavailable as error:
		raise HTTPException(status_code=503, detail={"code": "AI_RUNTIME_GOVERNANCE_UNAVAILABLE", "message": str(error)}) from error
	await _acquire_local_slot(clients.chat_semaphore, guard, lease)
	effective_policy = replace(
		policy,
		model_alias=lease.model_alias,
		fallback_reason=lease.fallback_reason or policy.fallback_reason,
	)
	model_metadata = policy.model_costs.get(lease.model_alias)
	environment = request.policy_context.environment if request.policy_context else settings.langfuse_environment
	if (
		(model_metadata and not model_metadata.get("supports_tools"))
		or (not model_metadata and environment in {"staging", "production"})
	):
		await _thread_call(
			guard.release, lease,
			actual_usage={"model_cost": _model_cost(policy, lease.model_alias)}, success=False,
		)
		clients.chat_semaphore.release()
		raise HTTPException(
			status_code=503,
			detail={"code": "AI_AGENT_MODEL_TOOLS_UNVERIFIED", "message": "Agent 模型尚未通过工具调用验证。"},
		)
	client = _client_for_policy(settings, effective_policy, clients)
	request = _with_runtime_policy_request(request, effective_policy)
	runtime = AgentRuntime(client, AgentToolClient(settings, async_client=clients.frappe))

	async def event_stream():
		released = False
		try:
			events = runtime.resume_stream(request) if resume else runtime.stream(request)
			async for event in events:
				if event.get("type") in {"started", "completed", "paused"}:
					if event.get("type") in {"completed", "paused"}:
						usage = event.get("usage") or {}
						cost, currency = _actual_cost(policy, lease.model_alias, usage)
						event.update({"estimated_cost": cost, "cost_currency": currency})
						await _thread_call(
							guard.release, lease,
							actual_usage={**usage, "model_cost": _model_cost(policy, lease.model_alias)},
							success=True,
						)
						released = True
					event.update({
						"policy_code": effective_policy.policy_code,
						"policy_version": effective_policy.policy_version,
						"fallback_reason": effective_policy.fallback_reason,
					})
				yield f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
		except AgentRuntimeError as error:
			await _thread_call(
				guard.release, lease,
				actual_usage={"model_cost": _model_cost(policy, lease.model_alias)}, success=False,
			)
			released = True
			event = {
				"type": "error", "code": error.code, "message": str(error),
				"retryable": error.retryable,
			}
			yield f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
		except httpx.HTTPStatusError:
			await _thread_call(guard.release, lease, actual_usage={"model_cost": _model_cost(policy, lease.model_alias)}, success=False, provider_failure=True)
			released = True
			event = {"type": "error", "code": "AGENT_PROVIDER_REJECTED", "message": "模型或工具服务拒绝了请求。"}
			yield f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
		except (httpx.HTTPError, RuntimeError, ValueError, json.JSONDecodeError):
			await _thread_call(guard.release, lease, actual_usage={"model_cost": _model_cost(policy, lease.model_alias)}, success=False, provider_failure=True)
			released = True
			event = {"type": "error", "code": "AI_AGENT_UNAVAILABLE", "message": "AI Agent 暂时不可用。"}
			yield f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
		finally:
			if not released:
				await _thread_call(guard.release, lease, actual_usage={"model_cost": _model_cost(policy, lease.model_alias)}, success=False)
			clients.chat_semaphore.release()

	return StreamingResponse(
		event_stream(),
		media_type="text/event-stream",
		headers={"Cache-Control": "no-cache, no-transform", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
	)


@app.post(
	"/internal/v1/agent/run/resume/stream",
	dependencies=[Depends(require_service_token)],
)
async def resume_stream_agent(
	request: AgentRequest,
	settings: Settings = Depends(get_settings),
	clients: RuntimeHttpClients = Depends(get_runtime_http_clients),
) -> StreamingResponse:
	return await stream_agent(request=request, settings=settings, clients=clients, resume=True)


@app.post(
	"/internal/v1/chat/stream",
	dependencies=[Depends(require_service_token)],
)
async def stream_chat(
	request: ChatRequest,
	settings: Settings = Depends(get_settings),
	clients: RuntimeHttpClients = Depends(get_runtime_http_clients),
) -> StreamingResponse:
	request = _validated_prompt_request(request)
	if len(request.messages) > settings.max_messages:
		raise HTTPException(status_code=422, detail="Too many messages")
	if any(len(message.content) > settings.max_message_chars for message in request.messages):
		raise HTTPException(status_code=422, detail="Message is too long")

	try:
		policy = await _thread_call(_policy_resolver.resolve, settings, request)
	except RuntimeError as error:
		raise HTTPException(status_code=503, detail={"code": "AI_RUNTIME_POLICY_UNAVAILABLE", "message": str(error)}) from error
	auto_model_requested = not request.model_alias
	policy = _with_requested_model(policy, request)
	policy = _with_required_modalities(policy, request)
	guard = _runtime_guard(settings)
	try:
		lease = await _thread_call(guard.select_and_acquire, policy, request)
	except RuntimeLimitExceeded as error:
		raise _limit_exception(error) from error
	except RuntimeControlUnavailable as error:
		raise HTTPException(status_code=503, detail={"code": "AI_RUNTIME_GOVERNANCE_UNAVAILABLE", "message": str(error)}) from error
	await _acquire_local_slot(clients.chat_semaphore, guard, lease)
	async def event_stream():
		released = False
		active_lease = lease
		try:
			while True:
				effective_policy = replace(
					policy,
					model_alias=active_lease.model_alias,
					fallback_reason=active_lease.fallback_reason or policy.fallback_reason,
				)
				client = _client_for_policy(settings, effective_policy, clients)
				effective_request = _with_runtime_policy_request(request, effective_policy)
				has_visible_output = False
				try:
					async for event in client.astream(effective_request):
						if event.get("type") == "message_delta" and event.get("delta"):
							has_visible_output = True
						if event.get("type") in {"started", "completed"}:
							if event.get("type") == "completed":
								usage = event.get("usage") or {}
								cost, currency = _actual_cost(policy, active_lease.model_alias, usage)
								event.update({"estimated_cost": cost, "cost_currency": currency})
								await _thread_call(
									guard.release,
									active_lease,
									actual_usage={
										**usage,
										"model_cost": _model_cost(policy, active_lease.model_alias),
									},
									success=True,
								)
								released = True
							event.update({
								"policy_code": effective_policy.policy_code,
								"policy_version": effective_policy.policy_version,
								"fallback_reason": effective_policy.fallback_reason,
							})
						yield f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
					return
				except (httpx.HTTPError, RuntimeError, json.JSONDecodeError) as error:
					await _thread_call(
						guard.release,
						active_lease,
						actual_usage={"model_cost": _model_cost(policy, active_lease.model_alias)},
						success=False,
						provider_failure=True,
					)
					if not has_visible_output and auto_model_requested:
						try:
							active_lease = await _thread_call(
								guard.acquire_fallback_after_failure,
								policy,
								request,
								active_lease.model_alias,
							)
							continue
						except (RuntimeLimitExceeded, RuntimeControlUnavailable):
							pass
					released = True
					if isinstance(error, httpx.HTTPStatusError):
						event = {
							"type": "error",
							**_provider_error_detail(ModelProviderRejected(
								model_alias=active_lease.model_alias,
								provider_status=error.response.status_code,
							)),
						}
					else:
						event = {
							"type": "error", "code": "AI_SERVICE_UNAVAILABLE",
							"message": "AI 服务暂时不可用。",
							"model_alias": active_lease.model_alias,
						}
					yield f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
					return
		finally:
			if not released:
				await _thread_call(
					guard.release,
					active_lease,
					actual_usage={"model_cost": _model_cost(policy, active_lease.model_alias)},
					success=False,
				)
			clients.chat_semaphore.release()

	return StreamingResponse(
		event_stream(),
		media_type="text/event-stream",
		headers={
			"Cache-Control": "no-cache, no-transform",
			"Connection": "keep-alive",
			"X-Accel-Buffering": "no",
		},
	)
