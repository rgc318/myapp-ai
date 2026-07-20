# 开发指南

## 1. 前置条件

- Python 3.12
- `uv`
- Docker Engine 与 Docker Compose v2
- Git

## 2. 获取与安装

```bash
git clone https://github.com/rgc318/myapp-ai.git
cd myapp-ai
uv sync --extra test --extra dev --frozen
```

`uv.lock` 是宿主机开发和安全审计的依赖事实源；Dockerfile 继续使用显式固定版本构建最小运行镜像。

## 3. 常用命令

```bash
uv run ruff check .
uv run pytest

docker build --target test -t myapp-ai:test .
docker run --rm myapp-ai:test
docker build --target runtime -t myapp-ai:runtime .
```

推荐使用 Docker test target 作为最终单元门禁，因为它与发布镜像使用同一基础层。

## 4. 独立集成测试

```bash
make integration
```

`integration.env` 只包含确定性的非生产测试值。测试栈启动 Redis、Qdrant、Orchestrator 和合成 OpenAI/Frappe Provider，验证健康、Chat、向量 upsert/search/delete，然后删除测试卷。默认 CI 不访问真实 ERP、不调用计费模型、不保存模型原文；真实运行仍必须从 `.env.example` 复制配置并替换全部 `change-me`。

## 5. 分支和提交

- `develop`：日常集成。
- `main`：稳定发布。
- `feature/*`、`fix/*`：从 `develop` 创建并通过 Pull Request 合回。

业务功能、部署配置和文档尽量分清责任，但同一契约变更必须在同一 PR 同步代码、测试和文档。禁止提交 `.env`、质量报告、备份、Token、Provider Key 或真实 ERP 数据。

## 6. 与父仓库联调

完整 ERP 联调仍在 `frappe_docker` 进行。AI 提交推送后，父仓库更新 `services/myapp-ai` gitlink；不要在父仓库直接产生未推送的 AI commit。
