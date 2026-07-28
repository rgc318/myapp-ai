from __future__ import annotations

import copy
import re
from dataclasses import dataclass

from .schemas import AgentRequest


class AgentRuntimeError(RuntimeError):
	def __init__(self, code: str, message: str, *, retryable: bool = False):
		super().__init__(message)
		self.code = code
		self.retryable = retryable


@dataclass(frozen=True, slots=True)
class GuardrailResult:
	phase: str
	status: str = "passed"
	code: str | None = None
	sanitized_fields: int = 0


_SECRET_REQUEST = re.compile(
	r"(?:system\s*prompt|系统提示词|开发者指令|service[_ -]?token|capability[_ -]?token|"
	r"api[_ -]?key|密钥|令牌).{0,40}(?:输出|显示|告诉|泄露|打印|返回|是什么|give|show|reveal|print)",
	re.IGNORECASE,
)
_SECRET_REQUEST_REVERSED = re.compile(
	r"(?:输出|显示|告诉|泄露|打印|返回|give|show|reveal|print).{0,40}(?:system\s*prompt|"
	r"系统提示词|开发者指令|service[_ -]?token|capability[_ -]?token|api[_ -]?key|密钥|令牌)",
	re.IGNORECASE,
)
_INSTRUCTIONAL_DATA = re.compile(
	r"(?:ignore|disregard|override).{0,30}(?:instruction|prompt)|"
	r"(?:忽略|无视|覆盖).{0,20}(?:之前|以上|系统|开发者).{0,20}(?:指令|提示)",
	re.IGNORECASE,
)
_OUTPUT_SECRET = re.compile(
	r"(?:\bBearer\s+[A-Za-z0-9._~+/=-]{20,}|\bsk-[A-Za-z0-9_-]{16,}|"
	r"MYAPP_AI_(?:SERVICE_TOKEN|LITELLM_API_KEY)\s*[:=])",
	re.IGNORECASE,
)
_OUTPUT_PROMPT_DISCLOSURE = re.compile(
	r"(?:系统提示词|system\s*prompt|开发者指令).{0,20}(?:如下|是|内容|begin|starts)",
	re.IGNORECASE,
)
_SENSITIVE_KEYS = {
	"authorization", "api_key", "apikey", "capability_token", "cookie", "password",
	"secret", "service_token", "token",
}


def check_agent_input(request: AgentRequest) -> GuardrailResult:
	text = "\n".join(message.content for message in request.messages if message.role == "user")
	if _SECRET_REQUEST.search(text) or _SECRET_REQUEST_REVERSED.search(text):
		raise AgentRuntimeError(
			"AI_AGENT_INPUT_BLOCKED",
			"请求包含获取系统指令或内部凭据的内容，已被安全策略阻止。",
		)
	return GuardrailResult(phase="input")


def _sanitize_value(value) -> tuple[object, int]:
	if isinstance(value, dict):
		result = {}
		changes = 0
		for key, child in value.items():
			if str(key).strip().lower() in _SENSITIVE_KEYS:
				changes += 1
				continue
			resolved, child_changes = _sanitize_value(child)
			result[key] = resolved
			changes += child_changes
		return result, changes
	if isinstance(value, list):
		result = []
		changes = 0
		for child in value:
			resolved, child_changes = _sanitize_value(child)
			result.append(resolved)
			changes += child_changes
		return result, changes
	if isinstance(value, str) and _INSTRUCTIONAL_DATA.search(value):
		return "[已移除工具结果中的指令式内容]", 1
	return value, 0


def sanitize_tool_result(result: dict) -> tuple[dict, GuardrailResult]:
	sanitized = copy.deepcopy(result)
	model_context, changes = _sanitize_value(sanitized.get("model_context") or {})
	sanitized["model_context"] = model_context
	return sanitized, GuardrailResult(
		phase="tool_output",
		status="sanitized" if changes else "passed",
		sanitized_fields=changes,
	)


def check_agent_output(content: str) -> GuardrailResult:
	if _OUTPUT_SECRET.search(content) or _OUTPUT_PROMPT_DISCLOSURE.search(content):
		raise AgentRuntimeError(
			"AI_AGENT_OUTPUT_BLOCKED",
			"模型输出触发敏感信息保护策略，已停止返回。",
		)
	return GuardrailResult(phase="output")
