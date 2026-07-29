from __future__ import annotations

import copy
import re
from dataclasses import dataclass

from .schemas import AgentRequest


class AgentRuntimeError(RuntimeError):
	def __init__(
		self, code: str, message: str, *, retryable: bool = False,
		details: list[str] | None = None,
	):
		super().__init__(message)
		self.code = code
		self.retryable = retryable
		self.details = list(details or [])


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
_IDENTIFIER_CLAIM = re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b")
_DATE_CLAIM = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_NUMBER_CLAIM = re.compile(r"(?<![A-Za-z0-9])([0-9][0-9,]*(?:\.[0-9]+)?)\s*([万千]?)")
_COMPANY_CLAIM = re.compile(r"(?:[A-Za-z][A-Za-z0-9 .&_-]{1,50}\sCompany|[\u3400-\u9fffA-Za-z0-9_-]{2,40}公司)")
_GENERIC_COMPANY_REFERENCE = re.compile(
	r"(?:您)?(?:当前)?(?:账号|用户)?(?:所在|所属|权限范围内的?)?公司|本公司|该公司"
)
_ABSOLUTE_COMPLETENESS = re.compile(r"(?:全部|所有|完整(?:结果|清单|数据)?|没有更多|仅有|总共)")
_STATUS_TERMS = {
	"completed": ("已完成", "完成", "completed"),
	"cancelled": ("已取消", "取消", "cancelled"),
	"draft": ("草稿", "draft"),
	"unfinished": ("未完成", "进行中", "unfinished"),
	"paid": ("已付款", "已支付", "paid"),
	"unpaid": ("未付款", "未支付", "unpaid"),
}
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


def _number_kind(path: str) -> str:
	key = path.lower()
	if any(term in key for term in ("amount", "price", "rate", "total", "outstanding", "paid", "received")):
		return "amount"
	if any(term in key for term in ("qty", "quantity", "stock")):
		return "quantity"
	if any(term in key for term in ("count", "limit")):
		return "count"
	return "number"


def _claim_number_kind(context: str, position: int) -> str:
	patterns = {
		"amount": r"(?:金额|销售额|采购额|售价|价格|单价|实收|应收|应付|元)",
		"quantity": r"(?:库存|数量|件|箱|个|套|公斤|千克)",
		"count": r"(?:返回|找到|结果|记录|条|项)",
	}
	candidates = []
	for kind, pattern in patterns.items():
		for match in re.finditer(pattern, context):
			center = (match.start() + match.end()) / 2
			candidates.append((abs(center - position), kind))
	return min(candidates)[1] if candidates else "number"


def _canonical_status(value: str) -> str | None:
	text = str(value or "").strip().lower()
	status_terms = (
		(canonical, term.lower())
		for canonical, terms in _STATUS_TERMS.items()
		for term in terms
	)
	for canonical, term in sorted(status_terms, key=lambda item: len(item[1]), reverse=True):
		if term in text:
			return canonical
	return None


def _claimed_statuses(content: str) -> set[str]:
	matches = []
	for canonical, terms in _STATUS_TERMS.items():
		for term in terms:
			for match in re.finditer(re.escape(term), content, flags=re.IGNORECASE):
				left = content[max(0, match.start() - 12):match.start()]
				right = content[match.end():match.end() + 12]
				context = r"(?:状态|订单|单据|付款状态|支付状态)"
				if re.search(context, left, flags=re.IGNORECASE) or re.search(
					context, right, flags=re.IGNORECASE,
				):
					matches.append((match.start(), match.end(), canonical))

	claimed = set()
	accepted_spans: list[tuple[int, int]] = []
	for start, end, canonical in sorted(
		matches, key=lambda item: (-(item[1] - item[0]), item[0]),
	):
		if any(start < accepted_end and end > accepted_start for accepted_start, accepted_end in accepted_spans):
			continue
		accepted_spans.append((start, end))
		claimed.add(canonical)
	return claimed


def _collect_grounding_value(
	value, *, path: str, strings: set[str], identifiers: set[str],
	numbers: dict[str, set[float]], statuses: set[str], companies: set[str],
) -> None:
	if isinstance(value, dict):
		for key, child in value.items():
			_collect_grounding_value(
				child, path=f"{path}.{key}", strings=strings, identifiers=identifiers,
				numbers=numbers, statuses=statuses, companies=companies,
			)
		return
	if isinstance(value, list):
		for index, child in enumerate(value):
			_collect_grounding_value(
				child, path=f"{path}[{index}]", strings=strings, identifiers=identifiers,
				numbers=numbers, statuses=statuses, companies=companies,
			)
		return
	if isinstance(value, bool) or value is None:
		return
	if isinstance(value, (int, float)):
		numbers.setdefault(_number_kind(path), set()).add(float(value))
		return
	text = str(value).strip()
	if not text:
		return
	strings.add(text)
	identifiers.update(_IDENTIFIER_CLAIM.findall(text.upper()))
	if "company" in path.lower():
		companies.add(text)
	if "status" in path.lower() and (canonical := _canonical_status(text)):
		statuses.add(canonical)


def check_agent_grounding(
	content: str, *, tool_results: list[dict], company: str,
) -> GuardrailResult:
	if not tool_results:
		return GuardrailResult(phase="output")
	strings: set[str] = set()
	identifiers: set[str] = set()
	numbers: dict[str, set[float]] = {}
	statuses: set[str] = set()
	companies = {str(company or "").strip()}
	completeness: list[bool | None] = []
	for index, result in enumerate(tool_results):
		grounding = result.get("grounding") or {}
		_collect_grounding_value(
			result.get("model_context") or {}, path=f"tool[{index}].model_context",
			strings=strings, identifiers=identifiers, numbers=numbers,
			statuses=statuses, companies=companies,
		)
		_collect_grounding_value(
			grounding, path=f"tool[{index}].grounding",
			strings=strings, identifiers=identifiers, numbers=numbers,
			statuses=statuses, companies=companies,
		)
		_collect_grounding_value(
			result.get("data") or {}, path=f"tool[{index}].data",
			strings=strings, identifiers=identifiers, numbers=numbers,
			statuses=statuses, companies=companies,
		)
		_collect_grounding_value(
			result.get("citations") or [], path=f"citations[{index}]",
			strings=strings, identifiers=identifiers, numbers=numbers,
			statuses=statuses, companies=companies,
		)
		for result_set in grounding.get("result_sets") or []:
			completeness.append(result_set.get("complete"))

	violations = []
	claimed_identifiers = set(_IDENTIFIER_CLAIM.findall(content.upper()))
	violations.extend(f"identifier:{value}" for value in sorted(claimed_identifiers - identifiers))
	claimed_dates = set(_DATE_CLAIM.findall(content))
	violations.extend(f"date:{value}" for value in sorted(value for value in claimed_dates if value not in strings))
	without_dates = _DATE_CLAIM.sub("", content)
	without_dates_or_identifiers = _IDENTIFIER_CLAIM.sub("", without_dates)
	allowed_all_numbers = set().union(*numbers.values()) if numbers else set()
	for match in _NUMBER_CLAIM.finditer(without_dates_or_identifiers):
		value = float(match.group(1).replace(",", ""))
		if match.group(2) == "万":
			value *= 10000
		elif match.group(2) == "千":
			value *= 1000
		window_start = max(0, match.start() - 12)
		context = without_dates_or_identifiers[window_start:match.end() + 12]
		kind = _claim_number_kind(context, match.start() - window_start)
		allowed = numbers.get(kind) or allowed_all_numbers
		if not any(abs(value - candidate) <= max(1e-9, abs(candidate) * 1e-9) for candidate in allowed):
			violations.append(f"{kind}:{value:g}")
	violations.extend(
		f"status:{canonical}" for canonical in sorted(_claimed_statuses(content) - statuses)
	)
	company_scan = content
	for allowed_company in sorted(companies, key=len, reverse=True):
		if allowed_company:
			company_scan = company_scan.replace(allowed_company, "")
	company_scan = _GENERIC_COMPANY_REFERENCE.sub("", company_scan)
	for claimed_company in _COMPANY_CLAIM.findall(company_scan):
		if claimed_company not in {"当前公司", "该公司"}:
			violations.append("company")
	if _ABSOLUTE_COMPLETENESS.search(content) and completeness and not all(value is True for value in completeness):
		violations.append("completeness")
	if violations:
		raise AgentRuntimeError(
			"AI_AGENT_OUTPUT_GROUNDING_FAILED",
			"模型输出包含无法由受控工具结果验证的业务事实。",
			details=sorted(set(violations)),
		)
	return GuardrailResult(phase="output")
