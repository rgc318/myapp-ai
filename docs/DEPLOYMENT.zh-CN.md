# 独立与组合部署

## 1. 独立本地部署

```bash
cp .env.example .env
# 替换全部 change-me，并配置可访问的 LiteLLM 与 Frappe 地址。
./scripts/standalone-up.sh
```

独立 Compose 启动 Orchestrator、Redis 和 Qdrant，只把 Orchestrator 绑定到 `127.0.0.1:4010`。Redis/Qdrant 位于内部网络，不发布宿主机端口。停止默认保留数据：

```bash
./scripts/standalone-down.sh
```

只有明确删除本地 Redis/Qdrant 数据时才执行：

```bash
./scripts/standalone-down.sh --volumes
```

## 2. 独立镜像

```bash
docker build --target runtime -t myapp-ai:runtime .
docker run --rm --env-file .env -p 127.0.0.1:4010:4010 myapp-ai:runtime
```

GitHub Release 或手工发布 workflow 推送 `ghcr.io/rgc318/myapp-ai` 的 amd64/arm64 镜像、provenance 和 SBOM。生产必须固定 digest。

## 3. 与 frappe_docker 组合

`frappe_docker` 通过 `services/myapp-ai` 子模块固定源码提交，用于本地 Dev Container 和可审计的 staging 构建。组合部署继续由父仓库提供 bundled Langfuse、Frappe Worker、跨服务 Secret 和完整回滚脚本。

```bash
git submodule update --init --recursive
./start-dev.sh
```

## 4. Staging

- 使用独立 ERP 与 AI 镜像标签。
- 不启动开发 bundled Langfuse；接入受控外部 Langfuse。
- 部署前验证 Provider、内部 Token、Redis、Qdrant alias、评测报告和 Secret 完整性。
- 同一发布记录必须能追溯父仓库 commit、AI commit、镜像 digest 和配置版本。

## 5. Production

- 至少两个 Orchestrator 副本，使用外部 Redis、Qdrant、LiteLLM、Langfuse 和负载均衡。
- Secret Manager 注入全部凭据；内部入口使用 TLS、网络策略或 mTLS 等价隔离。
- 使用不可变镜像 digest；部署先做 readiness，再滚动替换并保留回滚 digest。
- 单机 Compose 只适合开发和受控小规模环境，不代表生产 HA。
