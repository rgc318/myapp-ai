# myapp AI Orchestrator

独立的内部 AI 编排服务。当前提供受服务令牌保护的只读聊天接口，支持接收 Frappe 已鉴权并裁剪的业务上下文；它不直连 ERP 数据库、不持有 ERP 超级账号，也不包含正式单据写操作。

## 本地启动

只启动 Orchestrator：

```bash
docker compose \
  --env-file .env \
  --env-file .env.ai.local \
  up -d --build ai-orchestrator ai-vector
```

Orchestrator 的宿主机端口默认只绑定 `127.0.0.1:4010`。Web/Mobile 仍不得直连该端口。

## 环境变量

- `MYAPP_AI_LITELLM_BASE_URL`
- `MYAPP_AI_LITELLM_API_KEY`
- `MYAPP_AI_MODEL`
- `MYAPP_AI_REASONING_EFFORT`
- `MYAPP_AI_SERVICE_TOKEN`
- `MYAPP_AI_FRAPPE_BASE_URL`：仅用于读取受服务 Token 保护的已发布策略快照，默认 `http://backend:8000`
- `MYAPP_AI_FRAPPE_SITE_HOST`：Frappe 多站点路由 Host，当前本地站点为 `localhost`
- `MYAPP_AI_POLICY_CACHE_TTL_SECONDS`：已发布策略快照短缓存，默认 30 秒；刷新失败时只使用最后一个已验证快照
- `MYAPP_AI_REDIS_URL`：分布式 RPM/TPM、预算、并发租约和熔断状态；策略配置限制时缺失或不可用将失败关闭
- `MYAPP_AI_REDIS_KEY_PREFIX`：按环境隔离治理键，生产、staging 和开发不得共用前缀
- `MYAPP_AI_CIRCUIT_FAILURE_THRESHOLD / WINDOW_SECONDS / OPEN_SECONDS`：供应商超时、429、5xx 熔断阈值和窗口
- `MYAPP_AI_CONCURRENCY_LEASE_SECONDS`：异常退出时并发租约的自动回收时间
- `MYAPP_AI_TIMEOUT_SECONDS`
- `MYAPP_AI_EMBEDDING_MODEL`：LiteLLM Embedding 能力别名；未配置时向量能力保持关闭
- `MYAPP_AI_QDRANT_URL`
- `MYAPP_AI_QDRANT_COLLECTION`
- `MYAPP_AI_QDRANT_ALIAS`：在线检索/增量写入的稳定 alias；双 collection 发布前必须初始化并保持 Backend/Orchestrator 一致
- `MYAPP_AI_VECTOR_EXCLUDED_ITEM_PREFIXES`：Frappe 侧逗号分隔的明确测试商品编码前缀；只排除 AI 向量，不修改 ERP Item/历史交易
- `MYAPP_AI_VECTOR_TIMEOUT_SECONDS`
- `MYAPP_AI_VECTOR_SEARCH_ENABLED`：Frappe 侧显式开关，Embedding 冒烟通过前保持 `0`
- `MYAPP_AI_GOVERNANCE_LIVE_GATE_REPORT_PATH`：受控 live full-gate 报告路径；缺失、partial、失败、模型不一致或格式错误时禁止发布策略
- `MYAPP_AI_GOVERNANCE_EMBEDDING_GATE_REPORT_PATH`：受控 Embedding 质量/权限/恢复完整验收报告路径
- `MYAPP_AI_LANGFUSE_HOST`
- `MYAPP_AI_LANGFUSE_PUBLIC_KEY`
- `MYAPP_AI_LANGFUSE_SECRET_KEY`
- `MYAPP_AI_LANGFUSE_ENVIRONMENT`
- `MYAPP_AI_LANGFUSE_RELEASE`
- `MYAPP_AI_LANGFUSE_CAPTURE_CONTENT`：默认 `0`，只发送内容哈希和长度；明确通过数据治理评审后才能设为 `1`
- `MYAPP_AI_LANGFUSE_TIMEOUT_SECONDS`
- `MYAPP_AI_LANGFUSE_QUEUE_CAPACITY`：generation OTLP 有界内存队列容量，满时丢弃观测而不阻断 AI
- `MYAPP_AI_LANGFUSE_BATCH_SIZE` / `MYAPP_AI_LANGFUSE_FLUSH_INTERVAL_SECONDS`：后台批量大小与最大聚合等待时间
- `MYAPP_AI_LANGFUSE_MAX_RETRIES` / `MYAPP_AI_LANGFUSE_SHUTDOWN_TIMEOUT_SECONDS`：后台有限重试和优雅关闭排空上限

Langfuse 为可选、失败开放集成：未配置完整 host/public key/secret key 时不发送；generation OTLP 只在请求路径内执行 payload 构建和非阻塞入队，后台 Dispatcher 批量发送并有限重试，因此 Langfuse 慢响应不再增加 Chat/SSE 完成延迟。队列满、重试耗尽或关闭排空超时只增加丢弃指标，不阻断模型回复和 ERP 反馈保存。`/health` 的 `langfuse_delivery` 返回队列深度、成功、重试、失败和丢弃计数。用户 feedback score 仍在 ERP 本地保存后直接尝试同步，并明确返回 `observability_synced`。

Trace 使用 Frappe conversation/run 作为关联元数据，generation 记录模型、Token、延迟边界和错误，点赞/点踩同步为 score。Trace 的 `release`、generation 的 Prompt `version`、score 的 `environment/source` 均写入 Langfuse 原生字段；默认关闭原文采集时，反馈 comment 也只上传 SHA-256、字符数和字节数。

## 运行时策略、限流与熔断

已发布策略由 Orchestrator 通过专用内部 Token 读取，按场景、公司、角色和稳定灰度哈希解析。Redis Lua 在一次原子操作中检查并预留 RPM、TPM、日/月预算和并发租约；超限返回 HTTP 429、稳定错误码与 `Retry-After`。Redis 不可用时，带治理限制的策略失败关闭，不降级为各进程独立计数。

供应商超时、429 和 5xx 会累计模型熔断状态。熔断打开后仅允许一个 half-open 探测；非流式调用可在尚未返回内容时切换到策略中已验证的 fallback，SSE 开始输出后不会跨模型续写。预算动作 `use_lower_cost_fallback` 只切换到已登记成本更低的候选；所有回退原因写入 Run、Langfuse metadata 和每日聚合。

商品索引使用独立 `ai-vector` Frappe Worker，队列配置位于全局 `workers`。补偿/重建按 64 商品分批，单个 Orchestrator upsert 最多接收 128 个文档并执行一次 Qdrant upsert。若外部 Embedding provider 不支持数组输入，Orchestrator 使用最多 8 路的单条兼容降级并明确返回 `embedding_mode=parallel_single_fallback`。

## 商品向量语义检索

商品语义检索采用独立 Qdrant，镜像固定 digest，不发布宿主机端口；运行容器以 UID/GID `65534` 非 root 运行，rootfs 只读、capabilities 为空、启用 `no-new-privileges`，遥测关闭。一次性 `ai-vector-init` 仅以 `CHOWN` capability 初始化新数据卷权限，完成后退出。持久数据位于 `ai-vector-data` 卷，生产备份必须覆盖该卷或使用 Qdrant snapshot。

Embedding 版本发布使用新的物理 collection 和稳定 `MYAPP_AI_QDRANT_ALIAS`。Orchestrator 支持定向候选构建、collection/alias 状态读取和 `/collections/aliases` 原子切换；Frappe 保存逐商品构建状态、full-gate 证据、审批、发布和回滚审计。操作步骤见 `docs/codex/AI_VECTOR_RELEASE_RUNBOOK.zh-CN.md`。

索引文本只包含商品编码、名称、昵称、规格、用途描述、品牌、分类、条码和单位，不包含价格、库存、订单或其他交易数据。Qdrant 返回候选编码后，Frappe 会重新应用当前用户 Item 记录权限、公司范围、启停状态、销售/采购属性，并通过既有 `search_product_v2` 读取实时价格、库存和 UOM。向量服务异常时自动降级为关键词检索。

`MYAPP_AI_VECTOR_EXCLUDED_ITEM_PREFIXES=HTTP-` 会让明确测试商品跳过增量/重建/候选 collection，并从语义候选中二次过滤。管理员先调用 `cleanup_excluded_ai_product_vectors_v1(dry_run=true)` 核对范围，再带原因与幂等键执行清理；该操作只删除 Qdrant points 并更新向量状态，不删除或停用 ERP Item。

版本化中文质量集 `product-retrieval-zh-cn-v1` 包含 30 条直接、用途和模糊表达。真实运行：

```bash
MYAPP_AI_ENABLE_LIVE_EVALS=1 python -m myapp_ai.retrieval_quality \
  --output /tmp/product-retrieval-v1.json
```

报告同时检查 Top-1/Top-3、Provider 错误、排除候选泄漏和延迟；外部 Provider 失败时返回非零状态，不能作为发布通过证据。

启用步骤：

1. 在 LiteLLM 配置通过 `/v1/embeddings` 的 `erp-embedding` 能力别名。
2. 在 `.env.ai.local` 设置 `MYAPP_AI_EMBEDDING_MODEL=erp-embedding`。
3. 用单条合成商品完成 upsert/search/delete 冒烟。
4. 设置 `MYAPP_AI_VECTOR_SEARCH_ENABLED=1`，重建 backend、worker、scheduler 和 Orchestrator。
5. 执行 `bench --site localhost execute myapp.services.ai_vector_service.reconcile_product_vector_index` 分批补建索引。

`/health` 的 `vector_search_configured` 只有在 LiteLLM Key、Embedding 别名和 Qdrant URL 同时存在时才为 `true`。当前开关设计为显式启用，不能仅因 Qdrant 正常就宣称语义检索已上线。

若 `/v1/embeddings` 正常但所有 `/v1/chat/completions` 都以 `float() argument must be a string or a real number, not 'NoneType'` 失败，应检查 LiteLLM 全局 `litellm_settings.request_timeout` 是否为 `null`。该值必须配置为数值并重启 LiteLLM；这属于聊天路由配置，不代表 Qdrant 或 Embedding 故障。完整处理见 `docs/codex/KNOWN_ISSUES.zh-CN.md`。

## 本地 Langfuse

仓库提供隔离的 Langfuse v3.212.0 本地部署。首次启动前生成随机密钥和初始化账号：

```bash
./setup-ai-observability.sh
```

生成的 `.env.langfuse.local` 权限为 `0600` 且被 Git 忽略；脚本不会把密钥打印到终端，也不会覆盖已经存在的本地密钥文件。开发、测试和 Dev Container 默认启动 Orchestrator、Langfuse Web/Worker 及其独立 PostgreSQL、ClickHouse、Redis、MinIO：

```bash
./start-dev.sh
```

启动脚本会生成只包含 Frappe Gateway 必需字段的 `.env.ai.gateway.local`。Langfuse Web/Worker 应用配置、Web 初始化密钥、PostgreSQL、ClickHouse、Redis、MinIO 和 Orchestrator Project Key 分别进入权限为 `0600` 的忽略文件；四个存储容器只获得自身凭据，Worker 不获得初始化管理员密码，Orchestrator 不获得存储密钥。Backend、Frappe Worker 和 Scheduler 不会获得 LiteLLM 或 Langfuse 密钥。只有明确不需要本地观测时才使用 `./start-dev.sh --without-observability`。

Dev Container 同样默认包含六个 Langfuse 服务；首次构建前必须先运行一次 `./setup-ai-observability.sh`。Langfuse UI 默认访问 `http://127.0.0.1:3000`。

`start-prod.sh` 默认不启动本地 bundled Langfuse。正式生产应接入外部受控 Langfuse 和托管/HA 存储；只有明确接受单节点风险时才显式使用 `./start-prod.sh --with-observability`。三环境部署契约见 `docs/codex/AI_DEPLOYMENT_ENVIRONMENTS.zh-CN.md`。

健康检查：

```bash
curl -fsS http://127.0.0.1:3000/api/public/health
curl -fsS http://127.0.0.1:4010/health
```

Orchestrator 的 `/health` 会返回当前全部场景的 `prompt_versions`。运行镜像固定 Python 基础镜像 digest，并以 UID/GID `10001` 非 root 用户运行；Compose 同时启用只读根文件系统、`cap_drop: ALL`、`no-new-privileges` 和独立 `/tmp` tmpfs。

Langfuse UI 与 MinIO API 仅绑定 loopback；PostgreSQL、ClickHouse、Redis 和 MinIO Console 不发布宿主机端口。默认 `MYAPP_AI_LANGFUSE_CAPTURE_CONTENT=0`，只上传内容哈希和长度。自动初始化变量只保证首次空库创建组织、项目、账号和 API Key；修改环境文件不等于完成已有密钥轮换。

停止包含观测服务的完整本地栈并保留数据卷：

```bash
./stop.sh --with-observability
```

不要用 `down -v` 清理观测栈，除非明确要删除全部本地 trace、score 和账号数据。生产备份必须同时覆盖 PostgreSQL、ClickHouse 和 MinIO，不能只备份 PostgreSQL。

generation/trace 已迁移到 Langfuse OTLP HTTP `/api/public/otel/v1/traces`，使用 32 位 trace ID、generation observation、Prompt/模型/Token/Run/Conversation 元数据和默认内容哈希。用户反馈与固定评测 score 仍使用 score ingestion；其 HTTP 207 只有在逐事件 `errors` 为空且 `successes` 覆盖本批次全部事件 ID 时才算同步成功。备份、隔离恢复和内部服务 Token 轮换见 `docs/codex/AI_OBSERVABILITY_RECOVERY_RUNBOOK.zh-CN.md`。

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

模型策略发布不会直接信任浏览器上传的评测结论。将脱敏后的完整报告复制到宿主机 `ai-governance-reports/`，并通过 `.env.ai.local` 的治理报告路径指向容器内只读挂载。Orchestrator 会重新检查 Schema、full gate、阈值、模式和实际模型别名；未配置真实报告时即使 offline 21/21 也只允许保留草稿，不能审批发布。

ERP 商品、订单、库存和报表工具由 Frappe 在当前用户权限下执行，Orchestrator 只消费只读结果。无 ERP 数据的跨项目通用能力未来可以增加独立客户端入口，但当前内部 Bearer Token 不能交给浏览器。

## 接口

- `GET /health`
- `POST /internal/v1/chat`
- `POST /internal/v1/chat/stream`
- `POST /internal/v1/feedback`
- `POST /internal/v1/drafts/sales-order`
- `POST /internal/v1/drafts/purchase-order`
- `POST /internal/v1/drafts/inventory-adjustment`
- `POST /internal/v1/vector/products/upsert`
- `POST /internal/v1/vector/products/delete`
- `POST /internal/v1/vector/products/search`
- `POST /internal/v1/vector/products/status`
- `GET /internal/v1/governance/models`
- `POST /internal/v1/governance/validate-policy`

客户端未提供 Prompt 版本时，Orchestrator 会填入 registry 当前版本；只要显式提供的版本（包括空字符串）与当前版本不一致，聊天、流式和三类草稿接口都会返回 HTTP `409`，不会静默覆盖。

销售订单草稿接口优先请求严格 `json_schema`。模型供应商不支持时允许降级为 JSON-only，但响应仍必须通过同一 Pydantic Schema；Orchestrator 只返回候选字段，不解析或写入 ERP 主数据。

采购订单草稿使用独立 Schema，只提取供应商、采购商品、数量、单位、币种、仓库、日期和供应商参考号候选，不复用销售价格或客户字段。

库存调整草稿只提取单个库存商品、仓库、目标/增减数量、单位、日期和原因候选。实时库存、库存 UOM、换算、估值参考和目标差异由 Frappe 重新解析；接口不会创建或提交 `Stock Entry` / `Stock Reconciliation`。

流式接口返回标准 `text/event-stream`，事件包括 `started`、`message_delta`、`warning`、`completed` 和 `error`。模型供应商仍通过 LiteLLM OpenAI 兼容流式协议接入。

当前 myapp Web/Mobile 不得直接调用本服务或 LiteLLM；请求必须先经过 `myapp` Frappe AI Gateway。
