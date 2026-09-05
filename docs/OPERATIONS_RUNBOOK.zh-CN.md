# 运维运行手册

## 1. 启动与停止

```bash
./scripts/standalone-up.sh
python3 scripts/standalone_healthcheck.py
./scripts/standalone-down.sh
```

不要使用 `--volumes` 处理普通故障；它会删除 standalone Redis 和 Qdrant 数据。

## 2. 健康判定

健康分三层：

- `GET /livez` 只证明进程可以响应，用于重启判定。
- `GET /health` 返回兼容诊断快照、Release ID、协议以及 Prompt/Schema/工具 Manifest 摘要。
- `GET /readyz` 判断当前 Runtime 是否可以接单，并返回 7 个 Schema family、9 个场景兼容矩阵和 Release provenance；核心配置未就绪返回 HTTP 503，非关键治理能力缺失可以返回 `ready=true, status=degraded`。

`GET /health` 至少检查：

- `status=ok`
- `litellm_configured`
- `runtime_governance_configured`
- `vector_search_configured`
- `langfuse_delivery.worker_running/queue_depth/dropped_total`
- `prompt_versions`

Compose 容器健康使用 `/readyz`。它仍不代表真实 Provider 推理、Frappe 业务权限或 Embedding 质量已经通过；这些由定时 deep health、Backend 场景 readiness 和业务入口测试验证。

## 3. 常见故障

| 现象 | 处理 |
| --- | --- |
| 401 | 核对调用方/接收方 Token 版本，禁止把 Token 打到日志 |
| 409 / `AI_RUNTIME_CONTRACT_MISMATCH` | Backend 与 Orchestrator 协议主版本不兼容；成对同步制品，切换模型或重复请求无效 |
| 409 / `AI_SCHEMA_VERSION_MISMATCH` | 当前场景请求/响应 Schema 没有兼容交集；检查 `/readyz.schema_versions` 和兼容矩阵后同步制品 |
| 409 / `AI_PROMPT_VERSION_MISMATCH` | legacy 请求或 Agent resume 的 Prompt revision 不匹配；fresh request 不应再发送精确 Prompt |
| 429 / `AI_MODEL_HEALTH_HALF_OPEN_BUSY` | 旧 `unavailable` 已过期且另一个实例正在执行恢复探测；等待 `Retry-After`、观察 Provider 结果或使用已验证 fallback，不要重新写回永久不可用 |
| 422 / `AI_SELECTED_MODEL_INELIGIBLE` | 固定模型的生命周期、策略能力或工具能力不满足当前场景；刷新模型治理事实或改用自动策略，不得静默换模 |
| 503 / `AI_SCENARIO_MODEL_UNAVAILABLE` | 当前自动链没有任何场景合格模型；检查主模型和 fallback 的生命周期、能力探测、Policy 快照与当前环境 |
| 429 | 查看本地并发、Redis RPM/TPM/预算和 Provider 配额 |
| 502/503 Chat | 检查 LiteLLM 路由、Key、超时和熔断；不要归因于 Qdrant |
| 向量 503 | 检查 Embedding、Qdrant、维度和 collection/alias |
| Langfuse 丢弃增长 | 检查 Dispatcher/网络/Worker；AI 回复应继续成功 |
| Qdrant `Too many open files` | 核对 `nofile=65536`、连接和 snapshot 状态 |

模型健康排障时同时核对 Backend 注册表中的 `last_health_status`、`effective_health_status`、`last_health_at`、`health_expires_at`、`health_failure_count` 和 `last_health_trigger`。只有新鲜 `unavailable` 是硬阻断；`stale / unknown` 可直接尝试，`half_open` 受 Redis 15 秒恢复探测租约约束。若 Provider 已恢复但仍被阻断，先确认 Backend Policy 缓存失效调用是否成功，再确认当前运行快照而不是手工覆盖生命周期状态。

## 4. Token 轮换

1. 生成至少 256-bit 新 Token。
2. 先准备调用方和接收方双版本或受控同步窗口。
3. 更新 Secret Manager 并滚动重启。
4. 验证旧 Token 401、新 Token 200。
5. 报告只保存短哈希指纹。

## 5. Qdrant 恢复

使用 `scripts/qdrant_snapshot.py` 创建/下载 snapshot。恢复时使用隔离 collection，核对点数、维度、payload、质量集和权限二次过滤；验证完成后才允许切换 alias。

## 6. Langfuse 恢复

Langfuse PostgreSQL、ClickHouse、Redis 和对象存储必须形成一致恢复点。Standalone Compose 不包含 Langfuse 存储，恢复由受控 Langfuse 平台负责。观测恢复不得阻塞 ERP/AI 主链路恢复。

## 7. 升级和回滚

- 部署固定镜像 digest，记录旧/新 digest 和 AI commit。
- 发布前运行测试、集成、评测、安全和配置门禁。
- 失败时回滚镜像 digest；Embedding 问题独立回滚 alias。
- 回滚后复核 Token、Release ID、协议/Schema、策略快照和 Qdrant alias，不只检查容器 Up。
- 父部署仓启动后必须运行 `verify-ai-runtime-compatibility.sh`，以实际 Backend 和 Orchestrator 容器为准比较协议和全部 Schema family。Prompt revision 只作为审计展示，不再阻断 fresh request。
