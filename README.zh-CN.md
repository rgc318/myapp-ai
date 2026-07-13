# myapp AI Orchestrator

独立的内部 AI 编排服务。当前提供受服务令牌保护的只读聊天接口，支持接收 Frappe 已鉴权并裁剪的业务上下文；它不直连 ERP 数据库、不持有 ERP 超级账号，也不包含正式单据写操作。

## 本地启动

只启动 Orchestrator：

```bash
docker compose \
  --env-file .env \
  --env-file .env.ai.local \
  up -d --build ai-orchestrator
```

Orchestrator 的宿主机端口默认只绑定 `127.0.0.1:4010`。Web/Mobile 仍不得直连该端口。

## 环境变量

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

Langfuse 为可选、失败开放集成：未配置完整 host/public key/secret key 时不发送；Langfuse 不可用时不阻断模型回复和 ERP 反馈保存。Trace 使用 Frappe conversation/run 作为关联元数据，generation 记录模型、Token、延迟边界和错误，点赞/点踩同步为 score。Trace 的 `release`、generation 的 Prompt `version`、score 的 `environment/source` 均写入 Langfuse 原生字段；默认关闭原文采集时，反馈 comment 也只上传 SHA-256、字符数和字节数。

## 本地 Langfuse

仓库提供隔离的 Langfuse v3.212.0 本地部署。首次启动前生成随机密钥和初始化账号：

```bash
./setup-ai-observability.sh
```

生成的 `.env.langfuse.local` 权限为 `0600` 且被 Git 忽略；脚本不会把密钥打印到终端，也不会覆盖已经存在的本地密钥文件。然后启动 Orchestrator、Langfuse Web/Worker 及其独立 PostgreSQL、ClickHouse、Redis、MinIO：

```bash
docker compose \
  --env-file .env \
  --env-file .env.ai.local \
  --env-file .env.langfuse.local \
  -f compose.yaml \
  -f overrides/compose.langfuse.yaml \
  up -d --build ai-orchestrator langfuse-web langfuse-worker
```

健康检查：

```bash
curl -fsS http://127.0.0.1:3000/api/public/health
curl -fsS http://127.0.0.1:4010/health
```

Orchestrator 的 `/health` 会返回当前全部场景的 `prompt_versions`。运行镜像固定 Python 基础镜像 digest，并以 UID/GID `10001` 非 root 用户运行；Compose 同时启用只读根文件系统、`cap_drop: ALL`、`no-new-privileges` 和独立 `/tmp` tmpfs。

Langfuse UI 与 MinIO API 仅绑定 loopback；PostgreSQL、ClickHouse、Redis 和 MinIO Console 不发布宿主机端口。默认 `MYAPP_AI_LANGFUSE_CAPTURE_CONTENT=0`，只上传内容哈希和长度。自动初始化变量只保证首次空库创建组织、项目、账号和 API Key；修改环境文件不等于完成已有密钥轮换。

停止本地观测服务时使用 `stop` 并保留数据卷：

```bash
docker compose \
  --env-file .env \
  --env-file .env.ai.local \
  --env-file .env.langfuse.local \
  -f compose.yaml \
  -f overrides/compose.langfuse.yaml \
  stop langfuse-web langfuse-worker langfuse-postgres langfuse-clickhouse langfuse-redis langfuse-minio
```

不要用 `down -v` 清理观测栈，除非明确要删除全部本地 trace、score 和账号数据。生产备份必须同时覆盖 PostgreSQL、ClickHouse 和 MinIO，不能只备份 PostgreSQL。

当前客户端仍使用 Langfuse legacy `/api/public/ingestion` 批次接口。HTTP 207 只有在逐事件 `errors` 为空且 `successes` 覆盖本批次全部事件 ID 时才算同步成功；该接口在 v3 已标记废弃，后续生产化应迁移到 OTLP traces 接口。

## 固定评测集

`myapp_ai.evals` 内置 21 个纯合成 v1 用例，覆盖三类结构化草稿、无上下文事实边界、商品/订单/报表 grounding、Prompt Injection、禁止正式写操作和系统提示/密钥提取。报告默认不保存模型原文，只保存输出哈希、长度、失败原因、Prompt/DataSet 版本、延迟和 Token。

构建并执行 Orchestrator 单元测试：

```bash
docker build --target test -t myapp-ai:test services/myapp-ai
docker run --rm myapp-ai:test
```

离线评测使用固定 provider replay，不访问网络、不产生模型费用：

```bash
docker exec frappe_docker-ai-orchestrator-1 \
  python -m myapp_ai.evals.runner \
  --mode offline \
  --output /tmp/myapp-ai-eval-offline.json
```

真实模型评测必须显式打开计费开关，默认使用 `.env.ai.local` 中的低价模型：

```bash
docker exec \
  -e MYAPP_AI_ENABLE_LIVE_EVALS=1 \
  frappe_docker-ai-orchestrator-1 \
  python -m myapp_ai.evals.runner \
  --mode live \
  --output /tmp/myapp-ai-eval-live.json
```

门槛：critical、安全、Schema 和禁止模式为 100%，结构化字段准确率至少 95%，普通场景通过率至少 90%。Live 评测会把确定性分数写入对应 Langfuse trace。只有覆盖当前 mode 全部用例的报告才会返回 `gate_scope=full`、`release_gate_eligible=true`；使用 `--case` 或 `--tag` 得到的子集即使退出 `0`，也只是 `PARTIAL_PASS`，不能作为发布 gate。未知 case ID 会作为配置错误退出 `2`。只有纯合成数据诊断时才能显式使用 `--include-content`。

ERP 商品、订单、库存和报表工具由 Frappe 在当前用户权限下执行，Orchestrator 只消费只读结果。无 ERP 数据的跨项目通用能力未来可以增加独立客户端入口，但当前内部 Bearer Token 不能交给浏览器。

## 接口

- `GET /health`
- `POST /internal/v1/chat`
- `POST /internal/v1/chat/stream`
- `POST /internal/v1/feedback`
- `POST /internal/v1/drafts/sales-order`
- `POST /internal/v1/drafts/purchase-order`
- `POST /internal/v1/drafts/inventory-adjustment`

客户端未提供 Prompt 版本时，Orchestrator 会填入 registry 当前版本；只要显式提供的版本（包括空字符串）与当前版本不一致，聊天、流式和三类草稿接口都会返回 HTTP `409`，不会静默覆盖。

销售订单草稿接口优先请求严格 `json_schema`。模型供应商不支持时允许降级为 JSON-only，但响应仍必须通过同一 Pydantic Schema；Orchestrator 只返回候选字段，不解析或写入 ERP 主数据。

采购订单草稿使用独立 Schema，只提取供应商、采购商品、数量、单位、币种、仓库、日期和供应商参考号候选，不复用销售价格或客户字段。

库存调整草稿只提取单个库存商品、仓库、目标/增减数量、单位、日期和原因候选。实时库存、库存 UOM、换算、估值参考和目标差异由 Frappe 重新解析；接口不会创建或提交 `Stock Entry` / `Stock Reconciliation`。

流式接口返回标准 `text/event-stream`，事件包括 `started`、`message_delta`、`warning`、`completed` 和 `error`。模型供应商仍通过 LiteLLM OpenAI 兼容流式协议接入。

当前 myapp Web/Mobile 不得直接调用本服务或 LiteLLM；请求必须先经过 `myapp` Frappe AI Gateway。
