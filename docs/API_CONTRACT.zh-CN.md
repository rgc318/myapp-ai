# 内部 API 契约

## 1. 通用规则

- 除 `GET /health` 外，所有接口需要 `Authorization: Bearer <MYAPP_AI_SERVICE_TOKEN>`。
- 接口只服务受信任的 Frappe Gateway、后台 Worker 和受控运维工具，不是浏览器公开 API。
- JSON 字段以 Pydantic Schema 为事实源；客户端显式传入的旧 Prompt 版本返回 HTTP 409。
- `401` 表示 Token 错误，`409` 表示 Prompt 版本冲突，`422` 表示 Schema，`429` 表示有界背压，`502/503` 表示外部依赖拒绝或暂不可用。

## 2. 端点

| 方法与路径 | 用途 |
| --- | --- |
| `GET /health` | 配置、Prompt 版本、Langfuse Dispatcher 和能力健康 |
| `GET /internal/v1/governance/models` | 查询当前 LiteLLM Key 可见的完整模型库存及能力分类 |
| `POST /internal/v1/governance/models/availability` | 对指定或全部 LiteLLM 可见模型执行最小真实可用性探测 |
| `POST /internal/v1/governance/validate-policy` | 校验模型策略和受控评测报告 |
| `POST /internal/v1/chat` | 非流式受控业务回答 |
| `POST /internal/v1/chat/stream` | SSE 增量回答 |
| `POST /internal/v1/intent/parse` | 使用严格 JSON Schema 解析只读查询意图；不查询或写入 ERP |
| `POST /internal/v1/feedback` | 同步 Langfuse score，失败开放 |
| `POST /internal/v1/drafts/sales-order` | 销售订单候选草稿 |
| `POST /internal/v1/drafts/purchase-order` | 采购订单候选草稿 |
| `POST /internal/v1/drafts/inventory-adjustment` | 库存调整候选草稿 |
| `POST /internal/v1/drafts/product-setup` | 商品主数据、标准/批发/零售/成本价格与初始库存候选草稿 |
| `POST /internal/v1/vector/products/upsert` | 最多 128 个商品文档批量索引 |
| `POST /internal/v1/vector/products/delete` | 幂等删除商品 points |
| `POST /internal/v1/vector/products/search` | 语义候选检索 |
| `POST /internal/v1/vector/products/status` | 当前 collection 状态 |
| `POST /internal/v1/vector/governance/status` | collection 与 alias 治理状态 |
| `POST /internal/v1/vector/governance/switch-alias` | 原子切换 alias |
| `POST /internal/v1/vector/governance/validate-release` | 校验 Embedding 发布报告 |

## 3. Chat 请求最小示例

```json
{
  "messages": [{"role": "user", "content": "总结当前上下文"}],
  "scenario": "general",
  "user": "user@example.com",
  "company": "Example Company",
  "model_alias": "opencode-glm-5.2",
  "policy_context": {
    "roles": ["Sales User"],
    "environment": "staging"
  }
}
```

`model_alias` 可省略；省略时按已发布策略自动选择。显式提供时，调用方必须已经在 Frappe 模型注册表中校验该别名处于 `active / validated` 且属于聊天能力，Orchestrator 会固定使用该模型并关闭本次请求的静默模型降级。聊天、SSE 和四类结构化草稿共用这一选择语义。

`context` 只能由服务端加入，内容必须经过权限过滤和字段裁剪。模型文本不能作为商品编码、金额、库存、订单状态或权限判断的事实源。

`POST /internal/v1/intent/parse` 使用 `erp-intent-v3` Prompt 和严格 JSON Schema，返回 `general / product_search / order_query / report_summary`、置信度、商品实体、单据实体、报表口径、日期预设/明确起止日期、状态、排序、金额下限和数量。调用方可在服务端 `context.conversation_state` 中传入裁剪后的 `conversation-state-v1` 工作状态；当前消息优先，状态只用于解析省略和指代，不能作为实时业务事实。单据实体只允许 `sales_order`、`sales_invoice`、`purchase_order`、`purchase_invoice`；报表口径只允许 `overview`、`sales`、`purchase`、`cashflow`、`receivable_payable`。Frappe 仍会在执行边界重新校验日期顺序、金额范围、公司范围、DocType 白名单和权限；接口不可用、超时或输出不合法时必须回退本地规则。返回值只用于选择白名单查询服务，不能绕过权限、公司范围或业务参数校验。

`GET /internal/v1/governance/models` 会读取 LiteLLM `GET /v1/models`，返回当前 Service Key 可见的全部别名。配置的 Embedding 别名或名称包含 `embed / embedding` 的模型分类为 `embedding`，其余当前分类为 `fast_chat`；配置中存在但 LiteLLM 当前不可见的别名返回 `degraded / MODEL_ALIAS_NOT_FOUND`，供 Frappe 同步后阻止继续选择。

模型同步只证明别名对当前 `MYAPP_AI_LITELLM_API_KEY` 可见，不等于模型能够完成实际推理。`POST /internal/v1/governance/models/availability` 对 Chat 模型发送最多 8 个输出 Token 的最小请求，对 Embedding 模型发送一条固定合成文本；响应只保留可用状态、耗时、Provider 模型名和稳定错误码，不保存模型输出或 Provider 错误原文。该操作会产生少量真实 Provider 调用和费用。

## 4. SSE

响应类型为 `text/event-stream`，事件包括 `started`、`message_delta`、`warning`、`completed` 和 `error`。反向代理必须关闭缓冲并允许长连接；流已开始输出后不跨模型续写。

当前查询 Prompt 版本为 `erp-readonly-v7`。该版本将用户能力描述为“当前账号权限和公司范围内的受控业务查询”，并明确正式写操作必须由用户在业务页面确认；当调用方已经提供结构化业务结果时，回答不逐条复述记录或重新生成明细清单，只概括查询范围、数量和空结果。结果覆盖状态不等同于业务健康；没有明确异常字段时不得声称结果正常或无异常。

商品建档 Prompt 版本为 `product-setup-draft-v2`，可选提取 `standard_selling_rate`、`wholesale_rate`、`retail_rate`、`standard_buying_rate`；`valuation_rate` 仅保留旧响应兼容，不作为新成本字段。所有价格只来自用户明确表达，Orchestrator 不写 ERP，Frappe 仍需重新执行权限、主数据和价格校验。

## 5. 兼容性

增加可选字段属于向后兼容；删除字段、改变枚举、错误码或认证方式需要版本化端点。Prompt 版本是业务兼容边界，不能仅依赖 URL 版本。
