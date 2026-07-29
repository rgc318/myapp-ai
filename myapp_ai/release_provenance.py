from __future__ import annotations

import hashlib
import json

from .agent_tools import TOOL_REGISTRY
from .prompts import PROMPT_REGISTRY


def _canonical_sha256(value) -> str:
	serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
	return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def prompt_manifest() -> dict:
	prompts = {
		scenario: {
			"version": spec.version,
			"sha256": hashlib.sha256(spec.text.encode("utf-8")).hexdigest(),
		}
		for scenario, spec in sorted(PROMPT_REGISTRY.items())
	}
	return {
		"schema_version": "myapp-ai-prompt-manifest-v1",
		"prompts": prompts,
		"sha256": _canonical_sha256(prompts),
	}


def tool_manifest() -> dict:
	tools = {
		name: {
			"version": str(definition["version"]),
			"sha256": _canonical_sha256({
				"approval": definition.get("approval") or {},
				"type": definition["type"],
				"function": definition["function"],
			}),
		}
		for name, definition in sorted(TOOL_REGISTRY.items())
	}
	return {
		"schema_version": "myapp-ai-tool-manifest-v1",
		"tools": tools,
		"sha256": _canonical_sha256(tools),
	}
