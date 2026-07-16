# 配置参考

配置全部通过 `MYAPP_AI_*` 环境变量注入。`.env.example` 只含占位符，不可直接用于生产。

## 1. 核心与认证

| 变量 | 默认/要求 | 说明 |
| --- | --- | --- |
| `MYAPP_AI_SERVICE_TOKEN` | 必须为高熵 Secret | Frappe 与内部管理调用的 Bearer Token |
| `MYAPP_AI_LITELLM_BASE_URL` | `http://localhost:4000` | OpenAI-compatible Gateway 根地址 |
| `MYAPP_AI_LITELLM_API_KEY` | Chat/Embedding 必需 | 只进入 Orchestrator |
| `MYAPP_AI_MODEL` | `erp-fast-chat` | Chat 能力别名，不应绑定供应商物理模型名 |
| `MYAPP_AI_REASONING_EFFORT` | `none` | 供应商支持时传递 |
| `MYAPP_AI_TIMEOUT_SECONDS` | `60` | Chat/草稿读取超时 |
| `MYAPP_AI_MAX_COMPLETION_TOKENS` | `1200` | 默认最大输出 Token |

## 2. Frappe 策略

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MYAPP_AI_FRAPPE_BASE_URL` | `http://backend:8000` | 已发布策略快照 API |
| `MYAPP_AI_FRAPPE_SITE_HOST` | `localhost` | Frappe 多站点 Host |
| `MYAPP_AI_POLICY_CACHE_TTL_SECONDS` | `30` | 最后验证快照缓存，范围在代码内限制 |

Frappe 不可用且没有历史快照时，服务回退到无治理限制的系统默认模型，并返回 `policy_service_unavailable` 原因；一旦策略启用 Redis 限制，Redis 不可用会失败关闭。

## 3. Redis 与本地并发

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MYAPP_AI_REDIS_URL` | 空 | 分布式限流、预算、并发和熔断 |
| `MYAPP_AI_REDIS_KEY_PREFIX` | `myapp-ai` | 必须按环境隔离 |
| `MYAPP_AI_CHAT_CONCURRENCY` | `100` | 单进程 Chat/SSE 槽 |
| `MYAPP_AI_STRUCTURED_CONCURRENCY` | `20` | 单进程草稿槽 |
| `MYAPP_AI_EMBEDDING_CONCURRENCY` | `8` | 单进程 Embedding/检索槽 |

熔断阈值由 `MYAPP_AI_CIRCUIT_FAILURE_THRESHOLD`、`WINDOW_SECONDS`、`OPEN_SECONDS` 控制；并发租约由 `MYAPP_AI_CONCURRENCY_LEASE_SECONDS` 自动回收。

## 4. 向量检索

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MYAPP_AI_EMBEDDING_MODEL` | 空 | Embedding 能力别名；空表示关闭 |
| `MYAPP_AI_QDRANT_URL` | `http://ai-vector:6333` | Qdrant 地址 |
| `MYAPP_AI_QDRANT_COLLECTION` | `myapp-products-v1` | 物理 collection |
| `MYAPP_AI_QDRANT_ALIAS` | 空 | 推荐的在线稳定 alias |
| `MYAPP_AI_VECTOR_TIMEOUT_SECONDS` | `15` | Embedding/Qdrant 超时 |

## 5. Langfuse

`HOST`、`PUBLIC_KEY`、`SECRET_KEY` 三项必须全部配置才启用。`MYAPP_AI_LANGFUSE_CAPTURE_CONTENT` 默认 `0`；未完成数据治理评审不得设为 `1`。队列、批量、重试和排空变量详见 `OBSERVABILITY.zh-CN.md`。

## 6. Secret 规则

- Secret Manager 或编排平台负责生产注入与轮换。
- Backend/Worker 不获得 LiteLLM 或 Langfuse Secret。
- Orchestrator 不获得 Langfuse 数据库、ClickHouse、Redis 或对象存储根密钥。
- 日志、报告和 CI Artifact 不得输出 Secret 值；只允许不可逆短指纹。
