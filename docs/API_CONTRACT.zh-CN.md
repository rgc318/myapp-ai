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
| `GET /internal/v1/governance/models` | 查询 LiteLLM 已配置模型能力 |
| `POST /internal/v1/governance/validate-policy` | 校验模型策略和受控评测报告 |
| `POST /internal/v1/chat` | 非流式受控业务回答 |
| `POST /internal/v1/chat/stream` | SSE 增量回答 |
| `POST /internal/v1/feedback` | 同步 Langfuse score，失败开放 |
| `POST /internal/v1/drafts/sales-order` | 销售订单候选草稿 |
| `POST /internal/v1/drafts/purchase-order` | 采购订单候选草稿 |
| `POST /internal/v1/drafts/inventory-adjustment` | 库存调整候选草稿 |
| `POST /internal/v1/drafts/product-setup` | 商品主数据、标准售价与初始库存候选草稿 |
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
  "policy_context": {
    "roles": ["Sales User"],
    "environment": "staging"
  }
}
```

`context` 只能由服务端加入，内容必须经过权限过滤和字段裁剪。模型文本不能作为商品编码、金额、库存、订单状态或权限判断的事实源。

## 4. SSE

响应类型为 `text/event-stream`，事件包括 `started`、`message_delta`、`warning`、`completed` 和 `error`。反向代理必须关闭缓冲并允许长连接；流已开始输出后不跨模型续写。

当前查询 Prompt 版本为 `erp-readonly-v7`。该版本将用户能力描述为“当前账号权限和公司范围内的受控业务查询”，并明确正式写操作必须由用户在业务页面确认；当调用方已经提供结构化业务结果时，回答不逐条复述记录或重新生成明细清单，只概括查询范围、数量和空结果。结果覆盖状态不等同于业务健康；没有明确异常字段时不得声称结果正常或无异常。

## 5. 兼容性

增加可选字段属于向后兼容；删除字段、改变枚举、错误码或认证方式需要版本化端点。Prompt 版本是业务兼容边界，不能仅依赖 URL 版本。
