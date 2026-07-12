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

ERP 商品、订单、库存和报表工具由 Frappe 在当前用户权限下执行，Orchestrator 只消费只读结果。无 ERP 数据的跨项目通用能力未来可以增加独立客户端入口，但当前内部 Bearer Token 不能交给浏览器。

接口：

- `GET /health`
- `POST /internal/v1/chat`
- `POST /internal/v1/chat/stream`

流式接口返回标准 `text/event-stream`，事件包括 `started`、`message_delta`、`warning`、`completed` 和 `error`。模型供应商仍通过 LiteLLM OpenAI 兼容流式协议接入。

当前 myapp Web/Mobile 不得直接调用本服务或 LiteLLM；请求必须先经过 `myapp` Frappe AI Gateway。
