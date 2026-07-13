# myapp AI Orchestrator

独立的内部 AI 编排服务。当前提供受服务令牌保护的只读聊天接口，支持接收 Frappe 已鉴权并裁剪的业务上下文；它不直连 ERP 数据库、不持有 ERP 超级账号，也不包含正式单据写操作。

本地启动：

```bash
docker compose --env-file .env.ai.local up -d --build ai-orchestrator
```

环境变量：

- `MYAPP_AI_LITELLM_BASE_URL`
- `MYAPP_AI_LITELLM_API_KEY`
- `MYAPP_AI_MODEL`
- `MYAPP_AI_REASONING_EFFORT`
- `MYAPP_AI_SERVICE_TOKEN`
- `MYAPP_AI_TIMEOUT_SECONDS`
- `MYAPP_AI_LANGFUSE_HOST`
- `MYAPP_AI_LANGFUSE_PUBLIC_KEY`
- `MYAPP_AI_LANGFUSE_SECRET_KEY`
- `MYAPP_AI_LANGFUSE_ENVIRONMENT`
- `MYAPP_AI_LANGFUSE_RELEASE`
- `MYAPP_AI_LANGFUSE_CAPTURE_CONTENT`：默认 `0`，只发送内容哈希和长度；明确通过数据治理评审后才能设为 `1`
- `MYAPP_AI_LANGFUSE_TIMEOUT_SECONDS`

Langfuse 为可选、失败开放集成：未配置完整 host/public key/secret key 时不发送；Langfuse 不可用时不阻断模型回复和 ERP 反馈保存。Trace 使用 Frappe conversation/run 作为关联元数据，generation 记录模型、Token、延迟边界和错误，点赞/点踩同步为 score。

ERP 商品、订单、库存和报表工具由 Frappe 在当前用户权限下执行，Orchestrator 只消费只读结果。无 ERP 数据的跨项目通用能力未来可以增加独立客户端入口，但当前内部 Bearer Token 不能交给浏览器。

接口：

- `GET /health`
- `POST /internal/v1/chat`
- `POST /internal/v1/chat/stream`
- `POST /internal/v1/feedback`
- `POST /internal/v1/drafts/sales-order`
- `POST /internal/v1/drafts/purchase-order`

销售订单草稿接口优先请求严格 `json_schema`。模型供应商不支持时允许降级为 JSON-only，但响应仍必须通过同一 Pydantic Schema；Orchestrator 只返回候选字段，不解析或写入 ERP 主数据。

采购订单草稿使用独立 Schema，只提取供应商、采购商品、数量、单位、币种、仓库、日期和供应商参考号候选，不复用销售价格或客户字段。

流式接口返回标准 `text/event-stream`，事件包括 `started`、`message_delta`、`warning`、`completed` 和 `error`。模型供应商仍通过 LiteLLM OpenAI 兼容流式协议接入。

当前 myapp Web/Mobile 不得直接调用本服务或 LiteLLM；请求必须先经过 `myapp` Frappe AI Gateway。
