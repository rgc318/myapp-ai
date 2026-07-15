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
	qdrant_alias: str = ""
	vector_timeout_seconds: float = 15.0
	governance_live_gate_report_path: str = ""
	governance_embedding_gate_report_path: str = ""
	frappe_base_url: str = "http://backend:8000"
	frappe_site_host: str = "localhost"
	policy_cache_ttl_seconds: float = 30.0
	max_completion_tokens: int = 1200
	redis_url: str = ""
	redis_key_prefix: str = "myapp-ai"
	circuit_failure_threshold: int = 3
	circuit_failure_window_seconds: int = 60
	circuit_open_seconds: int = 60
	concurrency_lease_seconds: int = 180
	http_max_connections: int = 200
	http_max_keepalive_connections: int = 50
	http_keepalive_expiry_seconds: float = 30.0
	http_connect_timeout_seconds: float = 5.0
	http_pool_timeout_seconds: float = 2.0
	chat_concurrency: int = 100
	structured_concurrency: int = 20
	embedding_concurrency: int = 8

	@property
	def langfuse_enabled(self) -> bool:
		return bool(self.langfuse_host and self.langfuse_public_key and self.langfuse_secret_key)

	@property
	def vector_search_enabled(self) -> bool:
		return bool(self.litellm_api_key and self.embedding_model and self.qdrant_url)

	@property
	def active_qdrant_collection(self) -> str:
		return self.qdrant_alias or self.qdrant_collection


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
		qdrant_alias=_read_env("MYAPP_AI_QDRANT_ALIAS"),
		vector_timeout_seconds=float(_read_env("MYAPP_AI_VECTOR_TIMEOUT_SECONDS", "15")),
		governance_live_gate_report_path=_read_env("MYAPP_AI_GOVERNANCE_LIVE_GATE_REPORT_PATH"),
		governance_embedding_gate_report_path=_read_env("MYAPP_AI_GOVERNANCE_EMBEDDING_GATE_REPORT_PATH"),
		frappe_base_url=_read_env("MYAPP_AI_FRAPPE_BASE_URL", "http://backend:8000").rstrip("/"),
		frappe_site_host=_read_env("MYAPP_AI_FRAPPE_SITE_HOST", "localhost"),
		policy_cache_ttl_seconds=float(_read_env("MYAPP_AI_POLICY_CACHE_TTL_SECONDS", "30")),
		max_completion_tokens=int(_read_env("MYAPP_AI_MAX_COMPLETION_TOKENS", "1200")),
		redis_url=_read_env("MYAPP_AI_REDIS_URL"),
		redis_key_prefix=_read_env("MYAPP_AI_REDIS_KEY_PREFIX", "myapp-ai"),
		circuit_failure_threshold=int(_read_env("MYAPP_AI_CIRCUIT_FAILURE_THRESHOLD", "3")),
		circuit_failure_window_seconds=int(_read_env("MYAPP_AI_CIRCUIT_FAILURE_WINDOW_SECONDS", "60")),
		circuit_open_seconds=int(_read_env("MYAPP_AI_CIRCUIT_OPEN_SECONDS", "60")),
		concurrency_lease_seconds=int(_read_env("MYAPP_AI_CONCURRENCY_LEASE_SECONDS", "180")),
		http_max_connections=int(_read_env("MYAPP_AI_HTTP_MAX_CONNECTIONS", "200")),
		http_max_keepalive_connections=int(_read_env("MYAPP_AI_HTTP_MAX_KEEPALIVE_CONNECTIONS", "50")),
		http_keepalive_expiry_seconds=float(_read_env("MYAPP_AI_HTTP_KEEPALIVE_EXPIRY_SECONDS", "30")),
		http_connect_timeout_seconds=float(_read_env("MYAPP_AI_HTTP_CONNECT_TIMEOUT_SECONDS", "5")),
		http_pool_timeout_seconds=float(_read_env("MYAPP_AI_HTTP_POOL_TIMEOUT_SECONDS", "2")),
		chat_concurrency=int(_read_env("MYAPP_AI_CHAT_CONCURRENCY", "100")),
		structured_concurrency=int(_read_env("MYAPP_AI_STRUCTURED_CONCURRENCY", "20")),
		embedding_concurrency=int(_read_env("MYAPP_AI_EMBEDDING_CONCURRENCY", "8")),
	)
