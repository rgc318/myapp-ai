from __future__ import annotations

import re

from .agent_guardrails import AgentRuntimeError

TOOL_REGISTRY = {
	"search_products": {
		"version": "v1",
		"approval": {"required": False, "risk_level": "L1_READ_ONLY"},
		"type": "function",
		"function": {
			"name": "search_products",
			"description": "在当前用户与公司权限范围内查询商品。适用于名称、编码、条码、昵称、规格、包含某字或用途描述。只选择与用户条件相关的搜索字段；首次空结果时，可以去掉‘字样、商品、有没有’等查询外壳或切换匹配方式修正一次，第二次空结果必须停止。明确的单字符查询不得扩展或改写。",
			"strict": True,
			"parameters": {
				"type": "object",
				"properties": {
					"query": {"type": "string", "minLength": 1, "maxLength": 500, "description": "只保留实际商品查询词，例如‘带莫字’应传‘莫’。"},
					"match_mode": {"type": "string", "enum": ["auto", "exact", "contains", "semantic"]},
					"search_fields": {
						"type": "array",
						"items": {"type": "string", "enum": ["barcode", "item_code", "item_name", "nickname", "specification", "brand", "item_group"]},
						"maxItems": 7,
					},
					"limit": {"type": "integer", "minimum": 1, "maximum": 8},
				},
				"required": ["query", "match_mode", "search_fields", "limit"],
				"additionalProperties": False,
			},
		},
	},
	"query_business_documents": {
		"version": "v2",
		"approval": {"required": False, "risk_level": "L1_READ_ONLY"},
		"type": "function",
		"function": {
			"name": "query_business_documents",
			"description": "查询销售订单、销售发票、采购订单或采购发票的真实业务状态与金额。当前消息明确给出单据号，或明确指代 conversation_state 中唯一 resolved 的业务单据时，把该编号填入 document_name；其他列表查询必须传 null。",
			"strict": True,
			"parameters": {
				"type": "object",
				"properties": {
					"entities": {"type": "array", "items": {"type": "string", "enum": ["sales_order", "sales_invoice", "purchase_order", "purchase_invoice"]}, "minItems": 1, "maxItems": 4},
					"date_from": {"type": ["string", "null"], "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
					"date_to": {"type": ["string", "null"], "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
					"status": {"type": "string", "enum": ["all", "unfinished", "completed", "cancelled", "delivering", "receiving", "paying"]},
					"sort": {"type": "string", "enum": ["latest", "oldest", "amount_desc", "amount_asc"]},
					"min_amount": {"type": ["number", "null"], "minimum": 0},
					"limit": {"type": "integer", "minimum": 1, "maximum": 20},
					"document_name": {"type": ["string", "null"], "maxLength": 140},
				},
				"required": [
					"entities", "date_from", "date_to", "status", "sort", "min_amount", "limit",
					"document_name",
				],
				"additionalProperties": False,
			},
		},
	},
	"get_business_report": {
		"version": "v1",
		"approval": {"required": False, "risk_level": "L1_READ_ONLY"},
		"type": "function",
		"function": {
			"name": "get_business_report",
			"description": "查询经营总览、销售、采购、现金流或应收应付报表。",
			"strict": True,
			"parameters": {
				"type": "object",
				"properties": {
					"report_type": {"type": "string", "enum": ["overview", "sales", "purchase", "cashflow", "receivable_payable"]},
					"date_from": {"type": ["string", "null"], "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
					"date_to": {"type": ["string", "null"], "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
				},
				"required": ["report_type", "date_from", "date_to"],
				"additionalProperties": False,
			},
		},
	},
}


def tool_definitions(allowed_tools: list[str]) -> list[dict]:
	return [
		{"type": TOOL_REGISTRY[name]["type"], "function": TOOL_REGISTRY[name]["function"]}
		for name in allowed_tools if name in TOOL_REGISTRY
	]


def tool_approval_policy(tool: str) -> dict:
	definition = TOOL_REGISTRY.get(tool)
	if not definition:
		raise AgentRuntimeError("AI_AGENT_TOOL_UNAUTHORIZED", "模型请求了未授权的 Agent 工具。")
	return dict(definition.get("approval") or {"required": True, "risk_level": "L3_SENSITIVE"})


def _matches_type(value, expected) -> bool:
	types = expected if isinstance(expected, list) else [expected]
	for value_type in types:
		if value_type == "null" and value is None:
			return True
		if value_type == "string" and isinstance(value, str):
			return True
		if value_type == "array" and isinstance(value, list):
			return True
		if value_type == "object" and isinstance(value, dict):
			return True
		if value_type == "integer" and isinstance(value, int) and not isinstance(value, bool):
			return True
		if value_type == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
			return True
	return False


def _validate_schema(value, schema: dict, *, path: str) -> None:
	if not _matches_type(value, schema.get("type")):
		raise AgentRuntimeError("AI_AGENT_TOOL_ARGUMENTS_INVALID", f"工具参数 {path} 类型不正确。")
	if value is None:
		return
	if "enum" in schema and value not in schema["enum"]:
		raise AgentRuntimeError("AI_AGENT_TOOL_ARGUMENTS_INVALID", f"工具参数 {path} 不在允许范围内。")
	if isinstance(value, str):
		if len(value) < int(schema.get("minLength") or 0):
			raise AgentRuntimeError("AI_AGENT_TOOL_ARGUMENTS_INVALID", f"工具参数 {path} 不能为空。")
		if schema.get("maxLength") is not None and len(value) > int(schema["maxLength"]):
			raise AgentRuntimeError("AI_AGENT_TOOL_ARGUMENTS_INVALID", f"工具参数 {path} 超出长度限制。")
		if schema.get("pattern") and not re.fullmatch(str(schema["pattern"]), value):
			raise AgentRuntimeError("AI_AGENT_TOOL_ARGUMENTS_INVALID", f"工具参数 {path} 格式不正确。")
	if isinstance(value, (int, float)) and not isinstance(value, bool):
		if schema.get("minimum") is not None and value < schema["minimum"]:
			raise AgentRuntimeError("AI_AGENT_TOOL_ARGUMENTS_INVALID", f"工具参数 {path} 小于允许值。")
		if schema.get("maximum") is not None and value > schema["maximum"]:
			raise AgentRuntimeError("AI_AGENT_TOOL_ARGUMENTS_INVALID", f"工具参数 {path} 大于允许值。")
	if isinstance(value, list):
		if schema.get("minItems") is not None and len(value) < int(schema["minItems"]):
			raise AgentRuntimeError("AI_AGENT_TOOL_ARGUMENTS_INVALID", f"工具参数 {path} 项目过少。")
		if schema.get("maxItems") is not None and len(value) > int(schema["maxItems"]):
			raise AgentRuntimeError("AI_AGENT_TOOL_ARGUMENTS_INVALID", f"工具参数 {path} 项目过多。")
		for index, child in enumerate(value):
			_validate_schema(child, schema.get("items") or {}, path=f"{path}[{index}]")
	if isinstance(value, dict):
		properties = schema.get("properties") or {}
		missing = [name for name in schema.get("required") or [] if name not in value]
		if missing:
			raise AgentRuntimeError(
				"AI_AGENT_TOOL_ARGUMENTS_INVALID",
				f"工具参数缺少必填字段：{', '.join(missing)}。",
			)
		if schema.get("additionalProperties") is False:
			extra = sorted(set(value) - set(properties))
			if extra:
				raise AgentRuntimeError(
					"AI_AGENT_TOOL_ARGUMENTS_INVALID",
					f"工具参数包含未授权字段：{', '.join(extra)}。",
				)
		for name, child in value.items():
			if name in properties:
				_validate_schema(child, properties[name], path=f"{path}.{name}")


def validate_tool_arguments(tool: str, arguments: dict) -> dict:
	definition = TOOL_REGISTRY.get(tool)
	if not definition:
		raise AgentRuntimeError("AI_AGENT_TOOL_UNAUTHORIZED", "模型请求了未授权的 Agent 工具。")
	_validate_schema(arguments, definition["function"]["parameters"], path=tool)
	return arguments
