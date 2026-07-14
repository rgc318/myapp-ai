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
	langfuse_host: str = ""
	langfuse_public_key: str = ""
	langfuse_secret_key: str = ""
	langfuse_environment: str = "development"
	langfuse_release: str = ""
	langfuse_capture_content: bool = False
	langfuse_timeout_seconds: float = 5.0
	embedding_model: str = ""
	qdrant_url: str = ""
	qdrant_collection: str = "myapp-products-v1"
	vector_timeout_seconds: float = 15.0

	@property
	def langfuse_enabled(self) -> bool:
		return bool(self.langfuse_host and self.langfuse_public_key and self.langfuse_secret_key)

	@property
	def vector_search_enabled(self) -> bool:
		return bool(self.litellm_api_key and self.embedding_model and self.qdrant_url)


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
		langfuse_host=_read_env("MYAPP_AI_LANGFUSE_HOST").rstrip("/"),
		langfuse_public_key=_read_env("MYAPP_AI_LANGFUSE_PUBLIC_KEY"),
		langfuse_secret_key=_read_env("MYAPP_AI_LANGFUSE_SECRET_KEY"),
		langfuse_environment=_read_env("MYAPP_AI_LANGFUSE_ENVIRONMENT", "development"),
		langfuse_release=_read_env("MYAPP_AI_LANGFUSE_RELEASE"),
		langfuse_capture_content=_read_env("MYAPP_AI_LANGFUSE_CAPTURE_CONTENT", "0").lower()
		in {"1", "true", "yes"},
		langfuse_timeout_seconds=float(_read_env("MYAPP_AI_LANGFUSE_TIMEOUT_SECONDS", "5")),
		embedding_model=_read_env("MYAPP_AI_EMBEDDING_MODEL"),
		qdrant_url=_read_env("MYAPP_AI_QDRANT_URL", "http://ai-vector:6333").rstrip("/"),
		qdrant_collection=_read_env("MYAPP_AI_QDRANT_COLLECTION", "myapp-products-v1"),
		vector_timeout_seconds=float(_read_env("MYAPP_AI_VECTOR_TIMEOUT_SECONDS", "15")),
	)
