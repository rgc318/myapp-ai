# 运维运行手册

## 1. 启动与停止

```bash
./scripts/standalone-up.sh
python3 scripts/standalone_healthcheck.py
./scripts/standalone-down.sh
```

不要使用 `--volumes` 处理普通故障；它会删除 standalone Redis 和 Qdrant 数据。

## 2. 健康判定

`GET /health` 至少检查：

- `status=ok`
- `litellm_configured`
- `runtime_governance_configured`
- `vector_search_configured`
- `langfuse_delivery.worker_running/queue_depth/dropped_total`
- `prompt_versions`

容器健康只表示进程可响应，不代表 Provider、Frappe 策略、Embedding 质量或业务权限已通过。

## 3. 常见故障

| 现象 | 处理 |
| --- | --- |
| 401 | 核对调用方/接收方 Token 版本，禁止把 Token 打到日志 |
| 409 | 客户端 Prompt 版本过旧，刷新版本后重试 |
| 429 | 查看本地并发、Redis RPM/TPM/预算和 Provider 配额 |
| 502/503 Chat | 检查 LiteLLM 路由、Key、超时和熔断；不要归因于 Qdrant |
| 向量 503 | 检查 Embedding、Qdrant、维度和 collection/alias |
| Langfuse 丢弃增长 | 检查 Dispatcher/网络/Worker；AI 回复应继续成功 |
| Qdrant `Too many open files` | 核对 `nofile=65536`、连接和 snapshot 状态 |

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
- 回滚后复核 Token、Prompt 版本、策略快照和 Qdrant alias，不只检查容器 Up。
