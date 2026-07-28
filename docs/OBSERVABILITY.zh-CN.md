# 可观测性

## 1. Langfuse 契约

generation/trace 使用 Langfuse OTLP HTTP `/api/public/otel/v1/traces`；用户反馈和固定评测使用 score ingestion。只有 `MYAPP_AI_LANGFUSE_HOST`、`PUBLIC_KEY`、`SECRET_KEY` 全部配置时才启用。

Langfuse 是失败开放依赖：请求路径只构建脱敏 payload 并非阻塞入队，后台 Dispatcher 批量发送、有限重试和优雅排空。队列满、发送失败或关闭超时只增加丢弃指标，不改变模型回复结果。

## 2. 默认隐私

`MYAPP_AI_LANGFUSE_CAPTURE_CONTENT=0` 时仅上传输入、输出和反馈 comment 的 SHA-256、字符数和字节数。启用原文前必须完成数据分类、访问控制、保留期、删除、跨境和审计评审。

Trace 元数据包括：环境、release、模型/别名、Prompt 名称/版本、Token、延迟、Run、Conversation、策略版本、fallback 原因和错误类别。Agent 额外记录 `agent-run` 父 Span 与每次 `agent-tool:<tool>` 子 Span，包含 call_id、工具状态、结果数量和稳定错误码；默认内容采集关闭时只上传输入/输出摘要。不得写入 Service Token、Provider Key、能力令牌或完整业务上下文。

## 3. Dispatcher 参数

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MYAPP_AI_LANGFUSE_QUEUE_CAPACITY` | `1000` | 有界内存队列 |
| `MYAPP_AI_LANGFUSE_BATCH_SIZE` | `20` | 单批最大事件数 |
| `MYAPP_AI_LANGFUSE_FLUSH_INTERVAL_SECONDS` | `0.25` | 最大聚合等待 |
| `MYAPP_AI_LANGFUSE_MAX_RETRIES` | `2` | 后台有限重试 |
| `MYAPP_AI_LANGFUSE_SHUTDOWN_TIMEOUT_SECONDS` | `5` | 关闭排空上限 |

`/health.langfuse_delivery` 暴露 Worker、队列深度、入队、发送、批次失败、重试和丢弃累计值。

## 4. 告警

- Chat/SSE/草稿 5xx、429 和超时。
- Langfuse Worker 未运行、队列持续增长、失败或丢弃增长。
- Redis 不可用、预算拒绝和熔断打开。
- Qdrant p95、容量、snapshot 失败和 `nofile`。
- Provider 429/5xx、首 Token 和总延迟。

生产 Langfuse 的 PostgreSQL、ClickHouse、Redis 和对象存储必须作为一致恢复点管理；AI 仓库不持有这些存储根密钥。
