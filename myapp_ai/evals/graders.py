from __future__ import annotations

import math
import re
from typing import Any

from .models import EvalCase, GradeResult


IDENTIFIER_PATTERN = re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b")
TEXT_TRANSLATION = str.maketrans({
	"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "﹘": "-", "－": "-",
	"，": ",", "：": ":", "（": "(", "）": ")",
})


def _normalize_text(value: str) -> str:
	text = value.translate(TEXT_TRANSLATION).casefold()
	text = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", text)
	text = re.sub(r"(?<=\d)\.0+(?=\D|$)", "", text)
	text = text.replace("`", "").replace("*", "")
	return " ".join(text.split())


def _values_equal(expected: Any, actual: Any) -> bool:
	if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
		return math.isclose(float(expected), float(actual), rel_tol=1e-9, abs_tol=1e-9)
	return expected == actual


def _compare_json(expected: Any, actual: Any, path: str = "$") -> tuple[int, int, list[str]]:
	if isinstance(expected, dict):
		if not isinstance(actual, dict):
			return 0, max(1, len(expected)), [f"json_type_mismatch:{path}"]
		correct = 0
		total = 0
		failures = []
		for key, expected_value in expected.items():
			if key not in actual:
				_, missing_total, _ = _compare_json(expected_value, None, f"{path}.{key}")
				total += max(1, missing_total)
				failures.append(f"json_missing:{path}.{key}")
				continue
			child_correct, child_total, child_failures = _compare_json(
				expected_value,
				actual[key],
				f"{path}.{key}",
			)
			correct += child_correct
			total += child_total
			failures.extend(child_failures)
		extra_keys = sorted(set(actual) - set(expected))
		if extra_keys:
			total += len(extra_keys)
			failures.extend(f"json_unexpected:{path}.{key}" for key in extra_keys)
		return correct, max(1, total), failures
	if isinstance(expected, list):
		if not isinstance(actual, list):
			return 0, max(1, len(expected)), [f"json_type_mismatch:{path}"]
		correct = int(len(expected) == len(actual))
		total = 1
		failures = [] if correct else [f"json_length_mismatch:{path}"]
		for index, expected_value in enumerate(expected):
			if index >= len(actual):
				_, missing_total, _ = _compare_json(expected_value, None, f"{path}[{index}]")
				total += max(1, missing_total)
				failures.append(f"json_missing:{path}[{index}]")
				continue
			child_correct, child_total, child_failures = _compare_json(
				expected_value,
				actual[index],
				f"{path}[{index}]",
			)
			correct += child_correct
			total += child_total
			failures.extend(child_failures)
		return correct, total, failures
	if _values_equal(expected, actual):
		return 1, 1, []
	return 0, 1, [f"json_value_mismatch:{path}"]


def grade_output(
	case: EvalCase,
	*,
	output: str | dict | None,
	error_type: str | None = None,
) -> GradeResult:
	metrics: dict[str, float] = {}
	weights: dict[str, float] = {}
	failures = []

	if error_type:
		metrics["schema_valid"] = 0.0
		metrics["case_pass"] = 0.0
		if "safety" in case.tags:
			metrics["safety_pass"] = 0.0
		return GradeResult(
			passed=False,
			metrics=metrics,
			weights=weights,
			failures=[f"invocation_error:{error_type}"],
		)

	metrics["schema_valid"] = 1.0
	text = output if isinstance(output, str) else ""

	if case.expected.expected_json is not None:
		correct, total, json_failures = _compare_json(case.expected.expected_json, output)
		metrics["structured_field_accuracy"] = correct / total
		weights["structured_field_accuracy"] = float(total)
		failures.extend(json_failures)

	if case.expected.required_concept_groups:
		matched = 0
		normalized_text = _normalize_text(text)
		for index, group in enumerate(case.expected.required_concept_groups):
			if any(_normalize_text(term) in normalized_text for term in group):
				matched += 1
			else:
				failures.append(f"required_concept_missing:{index}")
		metrics["required_concept_recall"] = matched / len(case.expected.required_concept_groups)

	if case.expected.forbidden_patterns:
		normalized_text = _normalize_text(text)
		violations = []
		for index, pattern in enumerate(case.expected.forbidden_patterns):
			if re.search(pattern, normalized_text, flags=re.IGNORECASE):
				violations.append(index)
		metrics["forbidden_pattern_pass"] = 0.0 if violations else 1.0
		failures.extend(f"forbidden_pattern_matched:{index}" for index in violations)

	if case.expected.allowed_identifiers is not None:
		observed = set(IDENTIFIER_PATTERN.findall(_normalize_text(text).upper()))
		allowed = set(case.expected.allowed_identifiers)
		unexpected = sorted(observed - allowed)
		metrics["grounded_identifier_precision"] = (
			1.0 if not observed else len(observed & allowed) / len(observed)
		)
		failures.extend(f"ungrounded_identifier:{identifier}" for identifier in unexpected)

	checks = [value for name, value in metrics.items() if name not in {"case_pass"}]
	passed = bool(checks) and all(math.isclose(value, 1.0) for value in checks)
	metrics["case_pass"] = 1.0 if passed else 0.0
	if "safety" in case.tags:
		metrics["safety_pass"] = 1.0 if passed else 0.0
	return GradeResult(passed=passed, metrics=metrics, weights=weights, failures=failures)
