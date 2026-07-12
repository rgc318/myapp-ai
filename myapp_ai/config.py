from dataclasses import dataclass
import os


def _read_env(name: str, default: str = "") -> str:
	return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
	litellm_base_url: str
	litellm_api_key: str
	model: str
	reasoning_effort: str
	service_token: str
	timeout_seconds: float
	max_messages: int
	max_message_chars: int


def get_settings() -> Settings:
	return Settings(
		litellm_base_url=_read_env("MYAPP_AI_LITELLM_BASE_URL", "http://localhost:4000").rstrip("/"),
		litellm_api_key=_read_env("MYAPP_AI_LITELLM_API_KEY"),
		model=_read_env("MYAPP_AI_MODEL", "erp-fast-chat"),
		reasoning_effort=_read_env("MYAPP_AI_REASONING_EFFORT", "none"),
		service_token=_read_env("MYAPP_AI_SERVICE_TOKEN", "local-development-ai-service-token"),
		timeout_seconds=float(_read_env("MYAPP_AI_TIMEOUT_SECONDS", "60")),
		max_messages=int(_read_env("MYAPP_AI_MAX_MESSAGES", "20")),
		max_message_chars=int(_read_env("MYAPP_AI_MAX_MESSAGE_CHARS", "8000")),
	)
