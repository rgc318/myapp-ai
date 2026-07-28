# 配置参考

配置全部通过 `MYAPP_AI_*` 环境变量注入。`.env.example` 只含占位符，不可直接用于生产。

## 1. 核心与认证

| 变量 | 默认/要求 | 说明 |
| --- | --- | --- |
| `MYAPP_AI_SERVICE_TOKEN` | 必须为至少 32 字符的高熵 Secret | Frappe 与内部管理调用的 Bearer Token；缺失、过短或使用 `change-me` / `not-configured` 等占位值时服务拒绝启动 |
| `MYAPP_AI_LITELLM_BASE_URL` | `http://localhost:4000` | OpenAI-compatible Gateway 根地址 |
| `MYAPP_AI_LITELLM_API_KEY` | Chat/Embedding 必需 | 只进入 Orchestrator |
| `MYAPP_AI_MODEL` | `erp-fast-chat` | 自动策略不可用时的默认 Chat 别名；不限制 Frappe 用户从 LiteLLM 可见库存中选择其他合规模型 |
| `MYAPP_AI_REASONING_EFFORT` | `none` | 供应商支持时传递 |
| `MYAPP_AI_TIMEOUT_SECONDS` | `60` | Chat/草稿读取超时 |
| `MYAPP_AI_MAX_COMPLETION_TOKENS` | `1200` | 默认最大输出 Token |
| `MYAPP_AI_MAX_CONTEXT_TOKENS` | `24000` | 输入上下文总预算；保留系统提示、工具定义和输出预算后，从最近完整会话/工具调用单元向前裁剪 |
| `MYAPP_AI_AGENT_MAX_STEPS` | `3`，最大 `6` | 单个 Agent Run 的模型步骤预算 |
| `MYAPP_AI_AGENT_MAX_TOOL_CALLS` | `2`，最大 `6` | 单个 Agent Run 的工具调用预算 |
| `MYAPP_AI_AGENT_TOOL_TIMEOUT_SECONDS` | `20` | Frappe 工具回调超时 |
| `MYAPP_AI_AGENT_RUN_TIMEOUT_SECONDS` | `90`，范围 `5～300` | 覆盖模型决策、工具调用和最终生成的统一 Run deadline |
| `MYAPP_AI_AGENT_CANCEL_POLL_SECONDS` | `0.5`，范围 `0.2～5` | 模型请求等待期间向 Frappe 查询取消状态的间隔；取消后主动终止上游异步请求 |
| `MYAPP_AI_AGENT_MAX_TOTAL_TOKENS` | `60000`，范围 `1000～500000` | 单个 Agent Run 所有模型步骤累计 Token 上限 |

Agent 检查点没有可调大容量开关：Backend 固定限制单个 `agent-state-v1` 为 200KB、单个运行事件为 30KB。检查点写入和读取同时要求服务 Token 与当前 Run 能力令牌；不要通过扩大消息或工具结果绕过上下文和持久化边界。

## 2. Frappe 策略

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MYAPP_AI_FRAPPE_BASE_URL` | `http://backend:8000` | 已发布策略快照 API |
| `MYAPP_AI_FRAPPE_SITE_HOST` | `localhost` | Frappe 多站点 Host |
| `MYAPP_AI_POLICY_CACHE_TTL_SECONDS` | `30` | 最后验证快照缓存；新 Agent Run 在版本或模型元数据不一致时按需刷新 |
| `MYAPP_AI_POLICY_FAIL_OPEN` | `0` | 仅 development/test 可显式启用；staging/production 首次无法获取已验证策略时默认失败关闭 |

Frappe 不可用但存在历史已验证快照时继续使用 last-known-good，并返回 `stale_last_verified_snapshot`。没有历史快照时，staging/production 默认失败关闭；只有 development/test 显式设置 `MYAPP_AI_POLICY_FAIL_OPEN=1` 才允许回退无治理系统默认模型。一旦策略启用 Redis 限制，Redis 不可用同样失败关闭。

模型注册同步以当前 `MYAPP_AI_LITELLM_API_KEY` 调用 LiteLLM `/v1/models` 的结果为准，而不是只同步 `MYAPP_AI_MODEL` 和 `MYAPP_AI_EMBEDDING_MODEL`。请求显式带 `Cache-Control: no-cache`；但 LiteLLM 新增模型仍必须授权给该 Key，才会出现在同步结果中。因此 LiteLLM Key 的模型访问范围发生变化后，应重新执行 Frappe `sync_ai_model_registry_v1`；已消失的 LiteLLM 模型会在注册表中标记为 `degraded / missing`，不会继续出现在普通用户的可选列表中。

同步只检查 `/v1/models` 可见性。模型管理中的批量可用性检查会对 Chat/Embedding 端点执行最小真实请求；Chat 模型还会执行强制 Function Calling 探测并持久化 `supports_tools`。该检查产生少量 Provider 调用和费用，但不记录模型输出或 Provider 错误原文。staging/production Agent 只使用通过该探测的模型。

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
