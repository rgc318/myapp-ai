from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib import resources
import json
from pathlib import Path

from .models import EvalCase, ThresholdConfig


class EvalConfigurationError(RuntimeError):
	pass


@dataclass(frozen=True, slots=True)
class DatasetBundle:
	name: str
	version: str
	sha256: str
	cases: list[EvalCase]


def _read_text(name_or_path: str, *, suffix: str) -> tuple[str, str]:
	path = Path(name_or_path)
	if path.is_file():
		return path.read_text(encoding="utf-8"), str(path)

	filename = name_or_path
	if not filename.endswith(suffix):
		filename = f"{filename}.v1{suffix}"
	resource = resources.files("myapp_ai.evals.datasets").joinpath(filename)
	if not resource.is_file():
		raise EvalConfigurationError(f"Evaluation resource not found: {filename}")
	return resource.read_text(encoding="utf-8"), filename


def load_dataset(name_or_path: str = "core") -> DatasetBundle:
	text, source_name = _read_text(name_or_path, suffix=".jsonl")
	cases = []
	for line_number, raw_line in enumerate(text.splitlines(), 1):
		line = raw_line.strip()
		if not line or line.startswith("#"):
			continue
		try:
			cases.append(EvalCase.model_validate_json(line))
		except Exception as error:
			raise EvalConfigurationError(
				f"Invalid evaluation case at {source_name}:{line_number}: {type(error).__name__}"
			) from error
	if not cases:
		raise EvalConfigurationError(f"Evaluation dataset is empty: {source_name}")
	case_ids = [case.id for case in cases]
	if len(case_ids) != len(set(case_ids)):
		raise EvalConfigurationError(f"Evaluation dataset contains duplicate case ids: {source_name}")
	versions = {case.dataset_version for case in cases}
	if len(versions) != 1:
		raise EvalConfigurationError(f"Evaluation dataset mixes versions: {source_name}")
	return DatasetBundle(
		name=source_name,
		version=versions.pop(),
		sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
		cases=cases,
	)


def load_thresholds(name_or_path: str = "thresholds") -> ThresholdConfig:
	text, source_name = _read_text(name_or_path, suffix=".json")
	try:
		return ThresholdConfig.model_validate(json.loads(text))
	except Exception as error:
		raise EvalConfigurationError(
			f"Invalid evaluation thresholds at {source_name}: {type(error).__name__}"
		) from error
