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
