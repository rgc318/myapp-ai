# 配置参考

配置全部通过 `MYAPP_AI_*` 环境变量注入。`.env.example` 只含占位符，不可直接用于生产。

## 1. 核心与认证

| 变量                                  | 默认/要求                       | 说明                                                                                                         |
| ------------------------------------- | ------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `MYAPP_AI_SERVICE_TOKEN`              | 必须为至少 32 字符的高熵 Secret | Frappe 与内部管理调用的 Bearer Token；缺失、过短或使用 `change-me` / `not-configured` 等占位值时服务拒绝启动 |
| `MYAPP_AI_LITELLM_BASE_URL`           | `http://localhost:4000`         | OpenAI-compatible Gateway 根地址                                                                             |
| `MYAPP_AI_LITELLM_API_KEY`            | Chat/Embedding 必需             | 只进入 Orchestrator                                                                                          |
| `MYAPP_AI_MODEL`                      | `erp-fast-chat`                 | 自动策略不可用时的默认 Chat 别名；不限制 Frappe 用户从 LiteLLM 可见库存中选择其他合规模型                    |
| `MYAPP_AI_FALLBACK_MODELS`            | 空，逗号分隔                    | 系统默认策略的有序 Chat fallback alias；自动请求可使用，显式固定模型请求不会静默切换                         |
| `MYAPP_AI_REASONING_EFFORT`           | `none`                          | 供应商支持时传递                                                                                             |
| `MYAPP_AI_TIMEOUT_SECONDS`            | `60`                            | Chat/草稿读取超时                                                                                            |
| `MYAPP_AI_PROVIDER_MAX_ATTEMPTS`      | `3`                             | 在尚未返回内容或执行工具前，对同一固定模型的超时、网络错误、429 和 5xx 做有限重试，范围 1-3                    |
| `MYAPP_AI_PROVIDER_RETRY_BACKOFF_SECONDS` | `0.25`                      | 同模型瞬时重试的指数退避基数，范围 0-2 秒                                                                     |
| `MYAPP_AI_MAX_COMPLETION_TOKENS`      | `1200`                          | 默认最大输出 Token                                                                                           |
| `MYAPP_AI_MAX_CONTEXT_TOKENS`         | `24000`                         | 输入上下文总预算；保留系统提示、工具定义和输出预算后，从最近完整会话/工具调用单元向前裁剪                    |
| `MYAPP_AI_AGENT_MAX_STEPS`            | `3`，最大 `6`                   | 单个 Agent Run 的模型步骤预算                                                                                |
| `MYAPP_AI_AGENT_MAX_TOOL_CALLS`       | `2`，最大 `6`                   | 单个 Agent Run 的工具调用预算                                                                                |
| `MYAPP_AI_AGENT_TOOL_TIMEOUT_SECONDS` | `20`                            | Frappe 工具回调超时                                                                                          |
| `MYAPP_AI_AGENT_RUN_TIMEOUT_SECONDS`  | `90`，范围 `5～300`             | 覆盖模型决策、工具调用和最终生成的统一 Run deadline                                                          |
| `MYAPP_AI_AGENT_CANCEL_POLL_SECONDS`  | `0.5`，范围 `0.2～5`            | 模型请求等待期间向 Frappe 查询取消状态的间隔；取消后主动终止上游异步请求                                     |
| `MYAPP_AI_AGENT_MAX_TOTAL_TOKENS`     | `60000`，范围 `1000～500000`    | 单个 Agent Run 所有模型步骤累计 Token 上限                                                                   |

Agent 检查点没有可调大容量开关：Backend 固定限制单个 `agent-state-v1` 为 200KB、单个运行事件为 30KB。检查点写入和读取同时要求服务 Token 与当前 Run 能力令牌；不要通过扩大消息或工具结果绕过上下文和持久化边界。

`MYAPP_AI_FALLBACK_MODELS` 按声明顺序去重并忽略空值。它只补充“没有匹配已发布策略”时的系统默认链；命中已发布策略时仍以策略中的主模型和 fallback 为准。自动 Chat 会跳过最近健康状态为 `unavailable` 的候选，并且只允许在首个可见正文 Token 之前因 Provider 故障切换到后续模型，避免把两个模型的正文拼接到同一回答。浏览器显式提交 `model_alias` 时，本次请求固定到该模型并关闭静默 fallback。

无匹配已发布策略但浏览器显式提交固定模型时，Orchestrator 仍从 Frappe 模型快照读取该 alias 的健康、工具和 `supports_vision` 元数据，并将其加入 system-default 的运行元数据集合。固定模型不会因此成为自动 fallback；该元数据只用于正确完成健康、成本和图片能力校验。

## 2. Frappe 策略

| 变量                                           | 默认                  | 说明                                                                                                            |
| ---------------------------------------------- | --------------------- | --------------------------------------------------------------------------------------------------------------- |
| `MYAPP_AI_FRAPPE_BASE_URL`                     | `http://backend:8000` | 已发布策略快照 API                                                                                              |
| `MYAPP_AI_FRAPPE_SITE_HOST`                    | `localhost`           | Frappe 多站点 Host                                                                                              |
| `MYAPP_AI_POLICY_CACHE_TTL_SECONDS`            | `30`                  | 最后验证快照缓存；新 Agent Run 在版本或模型元数据不一致时按需刷新                                               |
| `MYAPP_AI_POLICY_FAIL_OPEN`                    | `0`                   | 仅 development/test 可显式启用；staging/production 首次无法获取已验证策略时默认失败关闭                         |
| `MYAPP_AI_RUNTIME_REVISION`                    | `unversioned`         | AI 镜像构建时注入的完整 commit/revision；治理报告必须与当前值完全一致，`unversioned` 不具备发布资格             |
| `MYAPP_AI_RELEASE_ID`                          | Runtime revision      | 当前跨服务发布标识；写入 `/health`、`/readyz`、业务响应和 Frappe Run，staging/production 应与不可变制品 tag 对齐 |
| `MYAPP_AI_GOVERNANCE_OFFLINE_GATE_REPORT_PATH` | 空                    | 已通过的确定性 Runtime full-gate 报告；运行服务只验证报告，不导入或执行 replay fixture                          |
| `MYAPP_AI_GOVERNANCE_LIVE_GATE_REPORT_PATH`    | 空                    | 已通过的真实 Provider full-gate 报告；必须绑定当前 Runtime、Prompt、工具、模型别名和与 offline 相同的数据集指纹 |

Frappe 不可用但存在历史已验证快照时继续使用 last-known-good，并返回 `stale_last_verified_snapshot`。没有历史快照时，staging/production 默认失败关闭；只有 development/test 显式设置 `MYAPP_AI_POLICY_FAIL_OPEN=1` 才允许回退无治理系统默认模型。一旦策略启用 Redis 限制，Redis 不可用同样失败关闭。

模型注册同步以当前 `MYAPP_AI_LITELLM_API_KEY` 调用 LiteLLM `/v1/models` 的结果为准，而不是只同步 `MYAPP_AI_MODEL` 和 `MYAPP_AI_EMBEDDING_MODEL`。请求显式带 `Cache-Control: no-cache`；但 LiteLLM 新增模型仍必须授权给该 Key，才会出现在同步结果中。因此 LiteLLM Key 的模型访问范围发生变化后，应重新执行 Frappe `sync_ai_model_registry_v1`；已消失的 LiteLLM 模型会在注册表中标记为 `degraded / missing`，不会继续出现在普通用户的可选列表中。

同步只检查 `/v1/models` 可见性。模型管理中的批量可用性检查会对 Chat/Embedding 端点执行最小真实请求；Chat 模型还会执行强制 Function Calling，以及红色、蓝色两张合成 PNG 的双图片挑战，并分别持久化 `supports_tools`、`supports_vision`、工具/视觉稳定错误码。视觉 Prompt 不包含预期答案，只要求模型返回图片中实际看到的单个小写英文颜色词；两张图片都精确命中才算通过，避免纯文本模型靠固定回答误判为支持视觉。视觉探测失败不会把仍可处理纯文本的模型整体标为不可用。该检查产生少量 Provider 调用和费用，但不记录模型输出、图片 base64 或 Provider 错误原文。staging/production Agent 只使用通过工具探测的模型，图片请求只使用通过视觉探测的模型。

## 3. Redis 与本地并发

| 变量                              | 默认       | 说明                         |
| --------------------------------- | ---------- | ---------------------------- |
| `MYAPP_AI_REDIS_URL`              | 空         | 分布式限流、预算、并发和熔断 |
| `MYAPP_AI_REDIS_KEY_PREFIX`       | `myapp-ai` | 必须按环境隔离               |
| `MYAPP_AI_CHAT_CONCURRENCY`       | `100`      | 单进程 Chat/SSE 槽           |
| `MYAPP_AI_STRUCTURED_CONCURRENCY` | `20`       | 单进程草稿槽                 |
| `MYAPP_AI_EMBEDDING_CONCURRENCY`  | `8`        | 单进程 Embedding/检索槽      |

熔断阈值由 `MYAPP_AI_CIRCUIT_FAILURE_THRESHOLD`、`WINDOW_SECONDS`、`OPEN_SECONDS` 控制；并发租约由 `MYAPP_AI_CONCURRENCY_LEASE_SECONDS` 自动回收。

## 4. 向量检索

| 变量                              | 默认                    | 说明                           |
| --------------------------------- | ----------------------- | ------------------------------ |
| `MYAPP_AI_EMBEDDING_MODEL`        | 空                      | Embedding 能力别名；空表示关闭 |
| `MYAPP_AI_QDRANT_URL`             | `http://ai-vector:6333` | Qdrant 地址                    |
| `MYAPP_AI_QDRANT_COLLECTION`      | `myapp-products-v1`     | 物理 collection                |
| `MYAPP_AI_QDRANT_ALIAS`           | 空                      | 推荐的在线稳定 alias           |
| `MYAPP_AI_VECTOR_TIMEOUT_SECONDS` | `15`                    | Embedding/Qdrant 超时          |

## 5. Langfuse

`HOST`、`PUBLIC_KEY`、`SECRET_KEY` 三项必须全部配置才启用。`MYAPP_AI_LANGFUSE_CAPTURE_CONTENT` 默认 `0`；未完成数据治理评审不得设为 `1`。队列、批量、重试和排空变量详见 `OBSERVABILITY.zh-CN.md`。

## 6. Secret 规则

- Secret Manager 或编排平台负责生产注入与轮换。
- Backend/Worker 不获得 LiteLLM 或 Langfuse Secret。
- Orchestrator 不获得 Langfuse 数据库、ClickHouse、Redis 或对象存储根密钥。
- 日志、报告和 CI Artifact 不得输出 Secret 值；只允许不可逆短指纹。
