# 发布流程

## 1. 版本与分支

- `develop` 接收功能、修复和依赖更新。
- `main` 只接收已验证的发布候选。
- Release tag 使用 `vMAJOR.MINOR.PATCH`。
- 破坏 API、Prompt、Schema、策略或向量空间兼容性的变更需要显式迁移和回滚方案。

## 2. 发布前门禁

```bash
uv sync --extra test --extra dev --frozen
uv run ruff check .
docker build --target test -t myapp-ai:test .
docker run --rm myapp-ai:test
make integration
```

同时要求依赖审计、Trivy、CodeQL、离线全量评测、必要的 live full gate、Compose 解析和文档更新通过。

## 3. 镜像发布

GitHub Release 发布或手工 workflow 生成：

- 版本 tag
- 完整 commit SHA tag
- amd64/arm64 manifest
- OCI source/revision 标签
- provenance
- SBOM

生产部署从 GHCR 解析并固定 digest，不使用可变 `latest` 作为审计证据。

## 4. 父仓库集成

AI 提交和镜像先发布；随后在 `frappe_docker/services/myapp-ai` 更新子模块指针，执行完整 Compose/staging 验证，再提交父仓库。顺序不能反转，否则父仓库会引用远程不存在的 AI commit。

## 5. 回滚

- 服务代码：恢复上一镜像 digest。
- Prompt/策略：通过 Frappe 已审计版本回滚。
- Embedding：原子把稳定 alias 指回旧 collection。
- Secret：恢复上一个仍有效版本，仅作为短期应急并立即重新轮换。

每次发布记录版本、AI commit、镜像 digest、父仓库 commit、配置版本、评测报告、审批人、发布时间和回滚窗口。
