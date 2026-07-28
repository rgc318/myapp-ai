# MyApp AI 文档中心

本目录是 AI Orchestrator 独立仓库的长期事实源。服务代码、配置、接口、测试、发布和通用运维规则在这里维护；`frappe_docker` 只维护整套 ERP 的 Compose、Dev Container、Langfuse bundled stack 和环境级部署细节。

## 文档索引

- 生产级 Agent Runtime：父部署仓库 `docs/codex/AI_AGENT_RUNTIME_ARCHITECTURE.zh-CN.md`

- [架构与边界](ARCHITECTURE.zh-CN.md)
- [开发指南](DEVELOPMENT.zh-CN.md)
- [配置参考](CONFIGURATION.zh-CN.md)
- [内部 API 契约](API_CONTRACT.zh-CN.md)
- [独立与组合部署](DEPLOYMENT.zh-CN.md)
- [安全设计](SECURITY.zh-CN.md)
- [可观测性](OBSERVABILITY.zh-CN.md)
- [向量检索与发布](VECTOR_SEARCH.zh-CN.md)
- [测试与评测](TESTING_AND_EVALUATION.zh-CN.md)
- [性能与容量](PERFORMANCE.zh-CN.md)
- [运维运行手册](OPERATIONS_RUNBOOK.zh-CN.md)
- [发布流程](RELEASE_PROCESS.zh-CN.md)

## 文档所有权

- AI 仓库负责：Orchestrator 代码、镜像、独立 Compose、接口、配置、安全、测试、性能和服务级运行手册。
- Frappe Backend 负责：用户认证、ERP 权限、公司范围、策略与审批、业务工具、正式单据和审计数据。
- `frappe_docker` 负责：完整 ERP 拓扑、Dev Container、bundled Langfuse、staging/production 编排和跨服务 Secret 分配。

接口、环境变量或运维行为发生变化时，必须在同一 Pull Request 更新对应文档。快速变化的本地结果不应覆盖长期契约；性能实测需注明日期、环境、模型和样本范围。
