# myapp AI Orchestrator

独立的内部 AI 编排服务。当前提供受服务令牌保护的生产级单 Agent Runtime：统一事件驱动 `AgentEngine` 同时服务同步、SSE、同步恢复和 SSE 恢复，模型通过正式 Function Calling 选择 Frappe 白名单工具，工具结果作为 `role=tool` 回传模型；输入、模型决策、待审批、工具完成和输出边界会写入 Frappe 持久检查点，可从同一 Run 的最近安全边界恢复。Frappe 工具结果携带 `agent-grounding-v1` 事实范围，最终回答在任何同步/SSE 内容可见前确定性校验业务标识符、数字、状态、公司和完整性；首次失败只允许一次无工具受控重写，再失败则关闭输出。敏感工具可在执行前持久暂停，批准或拒绝后继续原调用。服务不直连 ERP 数据库、不持有 ERP 超级账号；当前已注册工具仍全部只读，正式写操作继续使用草稿加人工确认。

服务同时支持文字与受控图片附件的多模态 Chat、意图识别和结构化商品/订单草稿。图片能力不根据模型名称推断：模型必须经过真实视觉健康检查并在 Runtime Policy 元数据中标记 `supports_vision=true`，才能接收图片请求。图片 base64 不进入默认 Langfuse input。完整契约见 `docs/API_CONTRACT.zh-CN.md`，业务设计见父仓库 `docs/05-development/06-ai-multimodal-product-and-order.zh-CN.md`。

## 仓库与交付边界

- 源码仓库：`https://github.com/rgc318/myapp-ai`
- 默认稳定分支：`main`；日常集成分支：`develop`
- 部署编排仓库：`https://github.com/rgc318/frappe_docker`
- 本仓库独立提供开发环境、依赖锁、Dockerfile、Redis/Qdrant Compose、合成集成测试、CI、安全门禁、GHCR 发布和服务级运维文档。
- 父仓库通过 `services/myapp-ai` Git 子模块固定 AI 源码提交，并负责完整 ERP Compose、Dev Container、bundled Langfuse、staging/production 和跨服务 Secret 编排。

独立开发可直接克隆本仓库；运行完整本地系统时应递归克隆部署仓库：

```bash
git clone https://github.com/rgc318/myapp-ai.git

git clone --recurse-submodules https://github.com/rgc318/frappe_docker.git
cd frappe_docker
git submodule update --init --recursive
```

对 AI 仓库的 `main` / `develop` push 和 Pull Request 会执行 Ruff、Docker 单元测试、runtime 构建、Standalone Compose 集成测试、安全审计和 CodeQL。正式发布通过 GitHub Release 或手工发布工作流生成 `ghcr.io/rgc318/myapp-ai` amd64/arm64 镜像，同时附带 provenance 和 SBOM。部署环境应固定镜像 digest 或父仓库中的子模块提交，不能依赖可变 `latest` 作为审计依据。

## 文档

完整文档索引位于 [`docs/README.zh-CN.md`](docs/README.zh-CN.md)，包括架构、开发、配置、API、部署、安全、观测、向量、评测、性能、运行手册和发布流程。

## 本地启动

独立启动：

```bash
cp .env.example .env
# 替换所有 change-me，并配置可访问的 LiteLLM 与 Frappe 地址。
./scripts/standalone-up.sh
python3 scripts/standalone_healthcheck.py
```

独立 Compose 启动 Orchestrator、Redis 和 Qdrant；Orchestrator 只绑定 `127.0.0.1:4010`，Redis/Qdrant 不发布宿主机端口。完整 ERP 联调在 `frappe_docker` 执行 `./start-dev.sh`。Web/Mobile 始终不得直连 Orchestrator。

## 环境变量

分类、默认值、Secret 边界和失败行为见 [`docs/CONFIGURATION.zh-CN.md`](docs/CONFIGURATION.zh-CN.md)。

- `MYAPP_AI_LITELLM_BASE_URL`
- `MYAPP_AI_LITELLM_API_KEY`
- `MYAPP_AI_MODEL`
- `MYAPP_AI_REASONING_EFFORT`
- `MYAPP_AI_SERVICE_TOKEN`
- `MYAPP_AI_FRAPPE_BASE_URL`：仅用于读取受服务 Token 保护的已发布策略快照，默认 `http://backend:8000`
- `MYAPP_AI_FRAPPE_SITE_HOST`：Frappe 多站点路由 Host，当前本地站点为 `localhost`
- `MYAPP_AI_POLICY_CACHE_TTL_SECONDS`：已发布策略快照短缓存，默认 30 秒；新 Agent Run 会校验 Frappe 预检命中的策略版本和相关模型元数据，不一致时按需强制刷新一次；刷新失败时只使用最后一个已验证且与请求期望一致的快照
- `MYAPP_AI_REDIS_URL`：分布式 RPM/TPM、预算、并发租约和熔断状态；策略配置限制时缺失或不可用将失败关闭
- `MYAPP_AI_REDIS_KEY_PREFIX`：按环境隔离治理键，生产、staging 和开发不得共用前缀
- `MYAPP_AI_CIRCUIT_FAILURE_THRESHOLD / WINDOW_SECONDS / OPEN_SECONDS`：供应商超时、429、5xx 熔断阈值和窗口
- `MYAPP_AI_CONCURRENCY_LEASE_SECONDS`：异常退出时并发租约的自动回收时间
- `MYAPP_AI_TIMEOUT_SECONDS`
- `MYAPP_AI_MAX_CONTEXT_TOKENS`：默认 `24000`，按估算 Token 保留最近完整会话与工具调用单元
- `MYAPP_AI_AGENT_MAX_STEPS / MYAPP_AI_AGENT_MAX_TOOL_CALLS`：Agent 有限循环预算
- `MYAPP_AI_AGENT_TOOL_TIMEOUT_SECONDS`：能力令牌工具回调超时
- `MYAPP_AI_AGENT_RUN_TIMEOUT_SECONDS / MYAPP_AI_AGENT_MAX_TOTAL_TOKENS`：单个 Run 的统一 deadline 与累计 Token 上限
- `MYAPP_AI_AGENT_CANCEL_POLL_SECONDS`：模型或流式请求等待期间的取消状态轮询间隔
- `MYAPP_AI_EMBEDDING_MODEL`：LiteLLM Embedding 能力别名；未配置时向量能力保持关闭
- `MYAPP_AI_QDRANT_URL`
- `MYAPP_AI_QDRANT_COLLECTION`
- `MYAPP_AI_QDRANT_ALIAS`：在线检索/增量写入的稳定 alias；双 collection 发布前必须初始化并保持 Backend/Orchestrator 一致
- `MYAPP_AI_VECTOR_EXCLUDED_ITEM_PREFIXES`：Frappe 侧逗号分隔的明确测试商品编码前缀；只排除 AI 向量，不修改 ERP Item/历史交易
- `MYAPP_AI_VECTOR_TIMEOUT_SECONDS`
- `MYAPP_AI_VECTOR_SEARCH_ENABLED`：Frappe 侧显式开关，Embedding 冒烟通过前保持 `0`
- `MYAPP_AI_RUNTIME_REVISION`：构建时注入的 AI commit/revision；受控报告与当前镜像不一致或仍为 `unversioned` 时禁止发布策略
- `MYAPP_AI_GOVERNANCE_OFFLINE_GATE_REPORT_PATH`：确定性 Runtime full-gate 报告路径；生产 Runtime 不现场导入或执行 replay fixture
- `MYAPP_AI_GOVERNANCE_LIVE_GATE_REPORT_PATH`：受控 live full-gate 报告路径；缺失、partial、失败、Runtime/Prompt/工具/模型/数据集不一致或格式错误时禁止发布策略
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

Embedding 版本发布使用新的物理 collection 和稳定 `MYAPP_AI_QDRANT_ALIAS`。Orchestrator 支持定向候选构建、collection/alias 状态读取和 `/collections/aliases` 原子切换；Frappe 保存逐商品构建状态、full-gate 证据、审批、发布和回滚审计。操作步骤见 [`docs/VECTOR_SEARCH.zh-CN.md`](docs/VECTOR_SEARCH.zh-CN.md)。

索引文本只包含商品编码、名称、昵称、规格、用途描述、品牌、分类、条码和单位，不包含价格、库存、订单或其他交易数据。Qdrant 返回候选编码后，Frappe 会重新应用当前用户 Item 记录权限、公司范围、启停状态、销售/采购属性，并通过既有 `search_product_v2` 读取实时价格、库存和 UOM。向量服务异常时自动降级为关键词检索。

`MYAPP_AI_VECTOR_EXCLUDED_ITEM_PREFIXES=HTTP-` 会让明确测试商品跳过增量/重建/候选 collection，并从语义候选中二次过滤。管理员先调用 `cleanup_excluded_ai_product_vectors_v1(dry_run=true)` 核对范围，再带原因与幂等键执行清理；该操作只删除 Qdrant points 并更新向量状态，不删除或停用 ERP Item。

版本化中文质量集 `product-retrieval-zh-cn-v1` 包含 30 条直接、用途和模糊表达。真实运行：

```bash
MYAPP_AI_ENABLE_LIVE_EVALS=1 python -m myapp_ai.retrieval_quality \
  --output /tmp/product-retrieval-v1.json
```

报告同时检查 Top-1/Top-3、Provider 错误、排除候选泄漏和延迟；外部 Provider 失败时返回非零状态，不能作为发布通过证据。

2026-07-15 23:57 CST 当前 v1 真实结果：单条/批量 Embedding 均 HTTP 200、1024 维；30 条中文门禁 Top-1 96.67%、Top-3 100%、Provider error 0、排除候选泄漏 0、p50 145.692ms、p95 211.745ms。该报告证明当前 `myapp-products-live` 可用，不代表新的 Embedding 向量空间已经完成候选 collection 发布门禁。

启用步骤：

1. 在 LiteLLM 配置通过 `/v1/embeddings` 的 `erp-embedding` 能力别名。
2. 独立部署在 `.env`、组合部署在父仓库 `.env.ai.local` 设置 `MYAPP_AI_EMBEDDING_MODEL=erp-embedding`。
3. 用单条合成商品完成 upsert/search/delete 冒烟。
4. 设置 `MYAPP_AI_VECTOR_SEARCH_ENABLED=1`，重建 backend、worker、scheduler 和 Orchestrator。
5. 执行 `bench --site localhost execute myapp.services.ai_vector_service.reconcile_product_vector_index` 分批补建索引。

`/health` 的 `vector_search_configured` 只有在 LiteLLM Key、Embedding 别名和 Qdrant URL 同时存在时才为 `true`。当前开关设计为显式启用，不能仅因 Qdrant 正常就宣称语义检索已上线。

若 `/v1/embeddings` 正常但所有 `/v1/chat/completions` 都以 `float() argument must be a string or a real number, not 'NoneType'` 失败，应检查 LiteLLM 全局 `litellm_settings.request_timeout` 是否为 `null`。该值必须配置为数值并重启 LiteLLM；这属于聊天路由配置，不代表 Qdrant 或 Embedding 故障。

## 本地 Langfuse

`frappe_docker` 提供隔离的 Langfuse v3.212.0 开发部署。以下命令在父部署仓库执行：

```bash
./setup-ai-observability.sh
```

生成的 `.env.langfuse.local` 权限为 `0600` 且被 Git 忽略；脚本不会把密钥打印到终端，也不会覆盖已经存在的本地密钥文件。开发、测试和 Dev Container 默认启动 Orchestrator、Langfuse Web/Worker 及其独立 PostgreSQL、ClickHouse、Redis、MinIO：

```bash
./start-dev.sh
```

启动脚本会生成只包含 Frappe Gateway 必需字段的 `.env.ai.gateway.local`。Langfuse Web/Worker 应用配置、Web 初始化密钥、PostgreSQL、ClickHouse、Redis、MinIO 和 Orchestrator Project Key 分别进入权限为 `0600` 的忽略文件；四个存储容器只获得自身凭据，Worker 不获得初始化管理员密码，Orchestrator 不获得存储密钥。Backend、Frappe Worker 和 Scheduler 不会获得 LiteLLM 或 Langfuse 密钥。只有明确不需要本地观测时才使用 `./start-dev.sh --without-observability`。

Dev Container 同样默认包含六个 Langfuse 服务；首次构建前必须先运行一次 `./setup-ai-observability.sh`。Langfuse UI 默认访问 `http://127.0.0.1:3000`。

`start-prod.sh` 默认不启动本地 bundled Langfuse。正式生产应接入外部受控 Langfuse 和托管/HA 存储；只有明确接受单节点风险时才显式使用 `./start-prod.sh --with-observability`。独立服务契约见 [`docs/OBSERVABILITY.zh-CN.md`](docs/OBSERVABILITY.zh-CN.md) 和 [`docs/DEPLOYMENT.zh-CN.md`](docs/DEPLOYMENT.zh-CN.md)。

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

generation/trace 已迁移到 Langfuse OTLP HTTP `/api/public/otel/v1/traces`，使用 32 位 trace ID、generation observation、Prompt/模型/Token/Run/Conversation 元数据和默认内容哈希。用户反馈与固定评测 score 仍使用 score ingestion；其 HTTP 207 只有在逐事件 `errors` 为空且 `successes` 覆盖本批次全部事件 ID 时才算同步成功。运行与恢复边界见 [`docs/OBSERVABILITY.zh-CN.md`](docs/OBSERVABILITY.zh-CN.md) 和 [`docs/OPERATIONS_RUNBOOK.zh-CN.md`](docs/OPERATIONS_RUNBOOK.zh-CN.md)。

## 固定评测集

`myapp_ai.evals` 内置 37 个纯合成 v1 用例，除最终回答、结构化意图与草稿外，还覆盖 Agent 在完整白名单中自主选择商品、业务单据和经营报表工具、模型生成参数、多工具组合、多轮工具切换、调用预算、空结果有限重试、上下文唯一订单精确查询和禁止越权工具；其中包含“带莫字商品”及语义变体。Agent 用例默认把三个已注册只读工具同时提供给模型，不能从 `expected_trajectory` 反推或缩窄候选工具。`expected_trajectory` 与 actual trajectory 分离：actual 只能从 `AgentEngine` 的真实模型决策和工具事件生成。报告默认不保存模型原文，只保存输出哈希、长度、轨迹评分、执行来源、失败原因、Prompt/DataSet 版本、延迟和 Token。

构建并执行 Orchestrator 单元测试：

```bash
docker build --target test -t myapp-ai:test .
docker run --rm myapp-ai:test
```

离线评测使用固定 provider replay 和合成 Frappe Tool API，不访问网络、不产生模型费用；Agent case 会实际运行完整 `AgentEngine`，不会把数据集预置轨迹冒充执行结果：

```bash
uv run python -m myapp_ai.evals.runner \
  --mode offline \
  --output /tmp/myapp-ai-eval-offline.json
```

真实模型评测必须显式打开计费开关，默认使用 `.env.ai.local` 中的低价模型；Agent critical case 使用真实模型 Function Calling 和合成 Tool Sandbox，不访问真实 ERP：

```bash
docker compose exec \
  -e MYAPP_AI_ENABLE_LIVE_EVALS=1 ai-orchestrator \
  python -m myapp_ai.evals.runner \
  --mode live \
  --output /tmp/myapp-ai-eval-live.json
```

门槛：critical、安全、Schema 和禁止模式为 100%，结构化字段准确率至少 95%，普通场景通过率至少 90%。每个 attempt 的 `execution_source` 区分 `provider_replay`、`agent_runtime_replay`、`live_provider` 和 `live_tool_sandbox`；未来 staging ERP 报告使用 `staging_erp`。Live 评测会把确定性分数写入对应 Langfuse trace。只有覆盖当前 mode 全部用例的报告才会返回 `gate_scope=full`、`release_gate_eligible=true`；使用 `--case` 或 `--tag` 得到的子集即使退出 `0`，也只是 `PARTIAL_PASS`，不能作为发布 gate。未知 case ID 会作为配置错误退出 `2`。只有纯合成数据诊断时才能显式使用 `--include-content`。

结构化意图的 `confidence` 只校验为 `[0,1]` 范围内的合法模型估计，不与 fixture 小数做精确相等比较；其余业务字段继续逐项校验。仅当用例在 `accepted_json_values` 中按 JSON 路径显式列出时，评测才允许某个非核心字段的等价值；未列出的值和其他字段仍精确校验。仅显式声明的安全用例可把 Provider 400/403 硬拒绝视为合格的无内容拒绝，普通用例的 HTTP、超时或连接错误仍失败，并以稳定错误码写入报告。

模型策略发布不会直接信任浏览器上传的评测结论。将脱敏后的完整报告复制到宿主机 `ai-governance-reports/`，并通过 `.env.ai.local` 的治理报告路径指向容器内只读挂载。Orchestrator 会重新检查 Schema、full gate、阈值、模式和实际模型别名；未配置真实报告时即使 offline 29/29 也只允许保留草稿，不能审批发布。

ERP 商品、订单、库存和报表工具由 Frappe 在当前用户权限下执行，Orchestrator 只消费只读结果。无 ERP 数据的跨项目通用能力未来可以增加独立客户端入口，但当前内部 Bearer Token 不能交给浏览器。

## 接口

- `GET /health`
- `POST /internal/v1/chat`
- `POST /internal/v1/chat/stream`
- `POST /internal/v1/intent/parse`
- `POST /internal/v1/feedback`
- `POST /internal/v1/drafts/sales-order`
- `POST /internal/v1/drafts/purchase-order`
- `POST /internal/v1/drafts/inventory-adjustment`
- `POST /internal/v1/drafts/product-setup`
- `POST /internal/v1/vector/products/upsert`
- `POST /internal/v1/vector/products/delete`
- `POST /internal/v1/vector/products/search`
- `POST /internal/v1/vector/products/status`
- `POST /internal/v1/vector/governance/status`
- `POST /internal/v1/vector/governance/switch-alias`
- `POST /internal/v1/vector/governance/validate-release`
- `GET /internal/v1/governance/models`
- `POST /internal/v1/governance/models/availability`
- `POST /internal/v1/governance/validate-policy`

客户端未提供 Prompt 版本时，Orchestrator 会填入 registry 当前版本；只要显式提供的版本（包括空字符串）与当前版本不一致，意图解析、聊天、流式和四类草稿接口都会返回 HTTP `409`，不会静默覆盖。

普通查询、商品、单据和报表场景当前使用 `erp-readonly-v8`。Prompt 将能力表述为当前账号权限和公司范围内的受控业务查询，正式业务写操作仍必须由用户在 ERP 页面确认；当上下文明示界面已经或将展示结构化明细时，模型只输出最多三个简短摘要要点，不逐条复述记录和字段，也不把结果数量覆盖状态误写成业务正常或无异常。上下文没有声明界面承担明细展示、且用户明确询问金额最高、最近或唯一结果时，模型应简要返回与问题直接相关的受控标识、金额和状态，不能只让用户自行查看界面。

销售订单草稿接口优先请求严格 `json_schema`。模型供应商不支持时允许降级为 JSON-only，但响应仍必须通过同一 Pydantic Schema；Orchestrator 只返回候选字段，不解析或写入 ERP 主数据。

商品创建/完善草稿使用 `product-setup-draft-v4`，新增 `operation=auto|create|update`，并区分标准售价、批发价、零售价和成本价（默认采购价）；旧 `valuation_rate` 仅用于兼容已有响应。模型只提取用户明确表达的意图和字段补丁；现有商品、价格、库存与执行前漂移由 Frappe 读取和校验。`currency` 只接受用户明确给出的 ISO 4217 代码或完整币种名称，金额后缀“元”或 `¥/￥` 不单独构成币种。

采购订单草稿使用独立 Schema，只提取供应商、采购商品、数量、单位、币种、仓库、日期和供应商参考号候选，不复用销售价格或客户字段。

库存调整草稿只提取单个库存商品、仓库、目标/增减数量、单位、日期和原因候选。实时库存、库存 UOM、换算、估值参考和目标差异由 Frappe 重新解析；接口不会创建或提交 `Stock Entry` / `Stock Reconciliation`。

流式接口返回标准 `text/event-stream`，事件包括 `started`、`message_delta`、`warning`、`completed` 和 `error`。模型供应商仍通过 LiteLLM OpenAI 兼容流式协议接入。

当前 myapp Web/Mobile 不得直接调用本服务或 LiteLLM；请求必须先经过 `myapp` Frappe AI Gateway。
