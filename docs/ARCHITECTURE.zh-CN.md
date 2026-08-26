# 架构与服务边界

## 1. 服务定位

MyApp AI Orchestrator 是独立 FastAPI 服务，负责模型调用、Prompt 版本、结构化草稿、运行时治理、向量客户端、评测和观测投递。它不导入 Frappe、ERPNext 或 `myapp` Python 包，也不连接 ERP 数据库。

```text
Web / Mobile
      |
      v
Frappe AI Gateway -- 鉴权、权限、公司范围、业务工具、正式写入
      |
      | internal Bearer token + minimized business context
      v
MyApp AI Orchestrator
   |        |         |          |
   v        v         v          v
LiteLLM   Redis     Qdrant    Langfuse
model     limits    vectors   optional telemetry
```

## 2. 不可变边界

- 浏览器和移动端不得持有内部服务 Token，也不得直连 Orchestrator。
- ERP 数据查询在 Frappe 当前用户上下文执行；Orchestrator 只接收裁剪后的只读结果。
- Orchestrator 不创建、提交或取消正式 ERP 单据。三个草稿接口只返回候选字段。
- 商品向量候选返回 Frappe 后，必须重新应用记录权限、公司、启停、销售/采购属性和实时价格库存。
- Prompt、模型策略和 Embedding collection 通过版本化与门禁发布，不能静默覆盖。
- Langfuse 失败开放；受治理的 Redis 限制失败关闭。

## 3. 运行依赖

| 依赖 | 必需性 | 责任 |
| --- | --- | --- |
| LiteLLM/OpenAI-compatible Gateway | Chat、草稿、Embedding 必需 | 模型能力别名、供应商认证和路由 |
| Frappe Backend | MyApp 业务运行必需 | 策略快照、权限、业务工具和审计 |
| Redis | 策略包含限流/预算/并发时必需 | 原子配额、租约和熔断 |
| Qdrant | 向量能力启用时必需 | 版本化 collection、alias 和检索 |
| Langfuse | 可选 | Trace、generation、score；故障不阻断回复 |

## 4. 可独立程度

仓库可以独立克隆、安装、测试、构建镜像、启动 Redis/Qdrant 依赖并运行合成集成测试。真实 MyApp 业务仍通过 API 契约依赖 Frappe，而不是依赖 Frappe 源码仓库。

## 5. 高可用边界

生产至少部署两个无状态 Orchestrator 副本；Redis、Qdrant、LiteLLM 和 Langfuse 使用外部受控高可用服务。负载均衡仅暴露内部网络入口，使用 `/health` 做存活检查，并在摘流后给予 SSE 和 Langfuse Dispatcher 有限排空时间。

## 6. 会话状态与工具编排

- 消息历史与工作状态分离：消息用于可审计对话，`conversation-state-v2` 只保存可序列化、受限大小的会话 scratchpad。该做法与 Google ADK 的 Session State（完整事件历史与动态 state 分离）及 Semantic Kernel 的 AgentThread（线程/会话状态抽象）一致。
- 状态使用 typed entity slots，而不是把“这个商品/订单”直接当数据库搜索词。Frappe 保存 `product`、`business_document`、`business_partner` 的稳定 ID、类型和 `resolved / ambiguous / not_found`，Orchestrator 只读取裁剪副本；正式工具仍重新查询权限和实时事实。
- Agent Runtime 不再丢弃全部会话状态，也不会接受任意业务上下文。它只白名单保留 `conversation-state-v2.active_entities` 中允许的类型、状态、稳定 ID 和显示名；`ambiguous / not_found` 的旧 ID 会被移除。该状态进入 `<conversation_state>` 后只能帮助选择工具和生成参数，工具结果仍是回答业务事实的唯一来源。
- 自动路由由严格结构化意图模型优先完成，确定性规则只承担不可绕过的策略边界：模型不可用/低置信度回退，以及防止明确写请求误入只读 Agent。业务关键词不直接执行数据库写入。
- Agent 的每个成功工具结果按顺序投影到线程状态；失败或拒绝信封不更新实体。多工具 Run 不只保留最后一个结果，可同时保留商品、单据和往来单位。
- 工具契约在 Orchestrator 和 Frappe 双重验证，包含额外字段拒绝、类型/枚举/数值范围、数组 `minItems/maxItems` 与跨字段约束。正式写入继续使用草稿、人工复核、幂等和审计，不开放模型直写 ERP。

参考：

- Google ADK Session State: <https://google.github.io/adk-docs/sessions/state/>
- Microsoft Semantic Kernel Agent Architecture: <https://learn.microsoft.com/en-us/semantic-kernel/frameworks/agent/agent-architecture>
