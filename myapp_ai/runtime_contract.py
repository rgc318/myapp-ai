from __future__ import annotations

from .prompts import prompt_versions
from .release_provenance import prompt_manifest, schema_manifest, tool_manifest

AI_RUNTIME_PROTOCOL_VERSION = "ai-runtime-contract-v1"
AI_RUNTIME_SUPPORTED_PROTOCOLS = (AI_RUNTIME_PROTOCOL_VERSION,)
AI_RUNTIME_CAPABILITIES = (
	"release-manifest-v1",
	"runtime-response-metadata-v1",
	"structured-contract-errors-v1",
)

AI_RUNTIME_SCHEMA_FAMILIES = {
	"agent": ("agent-runtime-v1",),
	"chat": ("chat-v1",),
	"intent_parse": ("intent-parse-v1",),
	"inventory_adjustment_draft": ("inventory-adjustment-draft-v1",),
	"product_setup_draft": ("product-setup-draft-v1",),
	"purchase_order_draft": ("purchase-order-draft-v1",),
	"sales_order_draft": ("sales-order-draft-v1",),
}

AI_RUNTIME_SCENARIO_SCHEMA_FAMILIES = {
	"general": ("chat", "agent"),
	"intent_parse": ("intent_parse",),
	"product_search": ("chat", "agent"),
	"order_query": ("chat", "agent"),
	"report_summary": ("chat", "agent"),
	"sales_order_draft": ("sales_order_draft",),
	"purchase_order_draft": ("purchase_order_draft",),
	"inventory_adjustment_draft": ("inventory_adjustment_draft",),
	"product_setup_draft": ("product_setup_draft",),
}


class RuntimeContractMismatchError(ValueError):
	def __init__(
		self, *, code: str, message: str, received: object = None, supported: object = None,
	):
		self.code = code
		self.received = received
		self.supported = supported
		super().__init__(message)


def negotiate_runtime_contract(request, *, schema_family: str):
	supported_schemas = AI_RUNTIME_SCHEMA_FAMILIES[schema_family]
	if not request.protocol_version:
		return request.model_copy(update={"schema_version": supported_schemas[0]})
	if request.protocol_version not in AI_RUNTIME_SUPPORTED_PROTOCOLS:
		raise RuntimeContractMismatchError(
			code="AI_RUNTIME_CONTRACT_MISMATCH",
			message="AI runtime protocol is not supported by the running orchestrator.",
			received=request.protocol_version,
			supported=list(AI_RUNTIME_SUPPORTED_PROTOCOLS),
		)
	offered_schemas = [str(value).strip() for value in request.supported_schema_versions if str(value).strip()]
	selected_schema = next((value for value in supported_schemas if value in offered_schemas), None)
	if not selected_schema:
		raise RuntimeContractMismatchError(
			code="AI_SCHEMA_VERSION_MISMATCH",
			message="AI request and response schemas are not compatible with the running orchestrator.",
			received=offered_schemas,
			supported=list(supported_schemas),
		)
	return request.model_copy(update={"schema_version": selected_schema})


def runtime_response_metadata(*, request, runtime_revision: str, release_id: str) -> dict:
	return {
		"protocol_version": AI_RUNTIME_PROTOCOL_VERSION,
		"schema_version": str(request.schema_version or ""),
		"prompt_version": str(request.prompt_version or ""),
		"runtime_revision": runtime_revision,
		"release_id": release_id,
	}


def runtime_contract_manifest(*, runtime_revision: str, release_id: str) -> dict:
	schemas = schema_manifest()
	return {
		"schema_version": "myapp-ai-runtime-contract-manifest-v1",
		"release_id": release_id,
		"protocol_version": AI_RUNTIME_PROTOCOL_VERSION,
		"supported_protocol_range": list(AI_RUNTIME_SUPPORTED_PROTOCOLS),
		"runtime_revision": runtime_revision,
		"orchestrator_revision": runtime_revision,
		"capabilities": list(AI_RUNTIME_CAPABILITIES),
		"prompt_versions": prompt_versions(),
		"schema_versions": {
			family: list(versions) for family, versions in AI_RUNTIME_SCHEMA_FAMILIES.items()
		},
		"compatibility_matrix": {
			scenario: {
				family: list(AI_RUNTIME_SCHEMA_FAMILIES[family])
				for family in families
			}
			for scenario, families in AI_RUNTIME_SCENARIO_SCHEMA_FAMILIES.items()
		},
		"prompt_manifest_sha256": prompt_manifest()["sha256"],
		"schema_manifest_sha256": schemas["sha256"],
		"tool_manifest_sha256": tool_manifest()["sha256"],
	}
