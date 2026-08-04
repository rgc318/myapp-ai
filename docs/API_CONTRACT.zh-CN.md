# 内部 API 契约

## 1. 通用规则

- 除 `GET /health` 外，所有接口需要 `Authorization: Bearer <MYAPP_AI_SERVICE_TOKEN>`。
- 接口只服务受信任的 Frappe Gateway、后台 Worker 和受控运维工具，不是浏览器公开 API。
- JSON 字段以 Pydantic Schema 为事实源；客户端显式传入的旧 Prompt 版本返回 HTTP 409。
- `401` 表示 Token 错误，`409` 表示 Prompt 版本冲突，`422` 表示 Schema，`429` 表示有界背压，`502/503` 表示外部依赖拒绝或暂不可用。
- 受治理 Chat、意图解析或结构化草稿的最终模型尝试被 Provider 拒绝时返回 HTTP 502，`detail.code=MODEL_PROVIDER_REJECTED`，并携带实际 `model_alias` 与可选 `provider_error_code=PROVIDER_HTTP_<status>`；不返回 Provider 原始正文。普通 Chat SSE 使用同字段的 `error` 事件。

## 2. 端点

| 方法与路径                                             | 用途                                                    |
| ------------------------------------------------------ | ------------------------------------------------------- |
| `GET /health`                                          | 配置、Prompt 版本、Langfuse Dispatcher 和能力健康       |
| `GET /internal/v1/governance/models`                   | 查询当前 LiteLLM Key 可见的完整模型库存及能力分类       |
| `POST /internal/v1/governance/models/availability`     | 对指定或全部 LiteLLM 可见模型执行最小真实可用性探测     |
| `POST /internal/v1/governance/validate-policy`         | 校验模型策略和受控评测报告                              |
| `POST /internal/v1/chat`                               | 非流式受控业务回答                                      |
| `POST /internal/v1/chat/stream`                        | SSE 增量回答                                            |
| `POST /internal/v1/agent/run`                          | 有限步单 Agent Runtime；模型使用正式 Function Calling   |
| `POST /internal/v1/agent/run/stream`                   | Agent 工具事件与最终答案真实上游 SSE                    |
| `POST /internal/v1/agent/run/resume`                   | 从 Frappe 持久安全检查点恢复同一 Agent Run              |
| `POST /internal/v1/agent/run/resume/stream`            | 以 SSE 恢复同一 Agent Run                               |
| `POST /internal/v1/intent/parse`                       | 使用严格 JSON Schema 解析只读查询意图；不查询或写入 ERP |
| `POST /internal/v1/feedback`                           | 同步 Langfuse score，失败开放                           |
| `POST /internal/v1/drafts/sales-order`                 | 销售订单候选草稿                                        |
| `POST /internal/v1/drafts/purchase-order`              | 采购订单候选草稿                                        |
| `POST /internal/v1/drafts/inventory-adjustment`        | 库存调整候选草稿                                        |
| `POST /internal/v1/drafts/product-setup`               | 商品主数据、标准/批发/零售/成本价格与初始库存候选草稿   |
| `POST /internal/v1/vector/products/upsert`             | 最多 128 个商品文档批量索引                             |
| `POST /internal/v1/vector/products/delete`             | 幂等删除商品 points                                     |
| `POST /internal/v1/vector/products/search`             | 语义候选检索                                            |
| `POST /internal/v1/vector/products/status`             | 当前 collection 状态                                    |
| `POST /internal/v1/vector/governance/status`           | collection 与 alias 治理状态                            |
| `POST /internal/v1/vector/governance/switch-alias`     | 原子切换 alias                                          |
| `POST /internal/v1/vector/governance/validate-release` | 校验 Embedding 发布报告                                 |

## 3. Chat 请求最小示例

```json
{
  "messages": [{ "role": "user", "content": "总结当前上下文" }],
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

模型同步只证明别名对当前 `MYAPP_AI_LITELLM_API_KEY` 可见，不等于模型能够完成实际推理。`POST /internal/v1/governance/models/availability` 对 Chat 模型先发送最小回答请求，再强制调用合成 `capability_probe` Function；分别返回 `available` 与 `supports_tools`。Embedding 模型发送一条固定合成文本。响应只保留能力、耗时、Provider 模型名和稳定错误码，不保存模型输出或 Provider 错误原文。该操作会产生少量真实 Provider 调用和费用。

Frappe 应先按注册表、人工状态和调用权限解析检测范围，再向 Orchestrator 发送明确 alias 列表：

```json
{
  "model_aliases": ["gpt-5.5", "opencode-deepseek-v4-flash"]
}
```

直接传空列表表示检查当前 LiteLLM Key 可见的全部模型；浏览器不得绕过 Frappe 直接使用这一语义。Orchestrator 会去重 alias，拒绝空字符串或超过 140 字符的值，单次最多 100 个。

响应示例：

```json
{
  "source": "litellm",
  "checked_count": 2,
  "available_count": 1,
  "unavailable_count": 1,
  "items": [
    {
      "model_alias": "gpt-5.5",
      "capability": "fast_chat",
      "available": true,
      "supports_tools": true,
      "latency_ms": 1580,
      "provider_model": "gpt-5.5",
      "error_code": null
    },
    {
      "model_alias": "opencode-deepseek-v4-flash",
      "capability": "fast_chat",
      "available": false,
      "supports_tools": false,
      "latency_ms": 8708,
      "provider_model": null,
      "error_code": "PROVIDER_HTTP_403"
    }
  ]
}
```

稳定错误码至少区分 Provider HTTP 拒绝、超时、网络错误、空响应、工具调用未返回和模型别名不存在。错误码用于治理、趋势和排障，不携带 Provider 原始响应正文。一次探测成功或失败都只是当时快照，不允许 Orchestrator 自动改变 Frappe 中的人工生命周期状态或发布策略。

Provider 拒绝示例：

```json
{
  "detail": {
    "code": "MODEL_PROVIDER_REJECTED",
    "message": "模型供应商拒绝了请求。",
    "model_alias": "opencode-deepseek-v4-flash",
    "provider_error_code": "PROVIDER_HTTP_403"
  }
}
```

`model_alias` 必须是本次最终尝试的实际 alias：固定模型使用请求 alias；自动策略使用最终选中的主模型或 fallback。Frappe 会把该字段写回 Run 并生成权限安全的 `model_display`。`provider_error_code` 是可选的稳定诊断码；Provider 没有返回 HTTP 状态但发生网络/运行时故障时，仍按现有 `503` 外部依赖故障处理，不能伪造 `PROVIDER_HTTP_*`。

`POST /internal/v1/chat/stream` 的 fallback 规则如下：

- 请求未携带 `model_alias` 时视为自动模式；Runtime Policy 的主模型和有序 fallback 均可参与选择。
- 最近健康状态为 `unavailable` 的候选在获取运行租约前跳过。
- Provider 在首个非空 `message_delta` 之前失败时，可以释放当前租约并获取后续 fallback；同一业务请求不会重复计入 RPM。
- 已经输出可见正文后不再切换模型，避免一条助手消息混合多个模型的内容。
- 请求显式携带 `model_alias` 时视为固定模式；Provider 失败直接返回该模型的错误，不允许静默 fallback。
- `started`、`completed` 和错误事件中的模型字段必须反映实际执行或最终失败尝试的 alias，不能继续报告最初不可用的主模型。

普通 Chat SSE 的错误事件示例：

```text
data: {"type":"error","code":"MODEL_PROVIDER_REJECTED","message":"模型供应商拒绝了请求。","model_alias":"opencode-deepseek-v4-flash","provider_error_code":"PROVIDER_HTTP_403"}
```

所有错误载荷都禁止包含 LiteLLM Key、Service Token、Authorization Header、Provider 原始正文或系统 Prompt。

## 4. Agent Runtime

Agent 请求除 Chat 字段外，必须携带 `run_id`、明确 `company`、短期 `capability_token` 和 `allowed_tools`。新 Run 还由 Frappe 携带 readiness 预检命中的 `policy_code / policy_version`；缺少有效握手时 Orchestrator 以 `AI_AGENT_POLICY_HANDSHAKE_REQUIRED` 拒绝请求。Orchestrator 先比较当前缓存快照，版本或相关模型元数据不一致时强制刷新一次，刷新后仍不一致则以 `AI_AGENT_POLICY_SNAPSHOT_MISMATCH` 失败关闭，不能使用旧策略执行。审批或失败检查点恢复继续绑定原 Run、能力范围、固定模型和持久化检查点，不重新冒充新 Run 握手。审批恢复时由 Frappe 额外携带服务端生成的 `approval` 决定，浏览器不能构造这些字段。能力令牌只发送给 Frappe 工具回调，不能进入模型消息、Langfuse input 或错误正文。当前白名单为 `search_products`、`query_business_documents`、`get_business_report`，全部只读；正式写操作仍使用草稿加人工确认链路。

模型返回的 `tool_calls` 必须命中本 Run 白名单，并通过与 Function Calling 定义相同的严格参数 Schema；额外字段、类型漂移、越界数值和非法枚举均在调用 Frappe 前阻断。Orchestrator 把 Frappe 结构化结果作为正式 `role=tool` 消息回传模型，并限制最大模型步骤、工具调用次数、统一 Run deadline、累计 Token、工具超时与上下文 Token 预算。staging/production 只允许模型注册表中 `supports_tools=true` 的已验证模型进入 Agent 路径。

同步、SSE、同步恢复和 SSE 恢复共享同一个事件驱动 `AgentEngine`；同步接口只收集规范化事件并生成 `AgentResponse`，SSE 接口只把相同事件映射为现有传输事件。首个模型决策可以使用有界非流式 Function Calling；产生首个正式工具结果后，后续模型步骤使用真实上游 SSE，同时允许继续返回增量 `tool_calls`。工具参数 delta 必须按调用索引完整聚合后再执行白名单和严格 Schema 校验；达到工具预算后下一模型步骤固定 `tool_choice=none` 形成最终回答。同步与 SSE 不得维护独立工具循环，也不得因传输方式不同而提前结束可继续的工具决策。

固定 Agent 评测同样调用 `AgentEngine`，数据集中的 `expected_trajectory` 只描述期望工具、参数和结果状态；provider replay 必须返回正式 Chat Completions `tool_calls`，工具沙箱必须返回正式 Frappe 工具信封。actual trajectory 只能从 Engine 终态事件中的真实 `tool_calls` 构造。评测报告以 `execution_source` 区分普通 provider replay、Agent Runtime replay、真实模型加合成 Tool Sandbox 和 staging ERP，禁止把 expected/replay trajectory 直接写成 actual。

`POST /internal/v1/governance/validate-policy` 不在生产进程中执行固定评测。它只读取 `myapp-ai-eval-report-v2` 的 offline/live full-gate 报告，并要求两者与当前 Runtime revision、完整 Prompt manifest、完整工具版本/Schema manifest、策略主模型和 fallback 别名顺序、数据集版本与 SHA-256 完全一致。runtime 镜像不包含 `myapp_ai.evals`；报告缺失、旧 Schema、`unversioned` Runtime 或任一 provenance 不匹配均失败关闭。

Frappe 工具回调为 `POST /api/method/myapp.api.gateway.execute_ai_agent_tool_v1`。它同时验证 `X-MyApp-AI-Service-Token` 与短期能力令牌，并按 `run_id + call_id` 幂等持久化结果。用户可通过 `cancel_ai_run_v1` 取消运行；取消会吊销能力令牌，迟到的完成或失败响应不能覆盖 `cancelled` 状态。Orchestrator 在等待模型或流式数据期间通过仅返回 `status/cancelled` 的内部 `GET /api/method/myapp.api.gateway.get_ai_agent_run_control_v1` 轮询控制状态，取消后会取消对应异步 HTTP 任务，而不只是修改数据库标记。

需要审批的工具在执行前调用内部 `request_ai_agent_tool_approval_v1`。请求必须携带原 `call_id`、工具、严格参数、风险等级、`waiting_approval` 检查点和能力令牌；Frappe 以服务端工具策略为权威，在同一事务中保存检查点、参数哈希和裁剪摘要，创建审批记录，把 Run 切到 `waiting_approval` 并吊销令牌。同步响应的 `status=waiting_approval` 携带 `approval`；SSE 依次发送 `approval_required` 和 `paused`。批准或拒绝后恢复请求携带原审批决定并继续相同 Run，不能重新生成或替换工具参数。

输入、工具结果和输出均经过独立 Guardrail。输入侧阻断系统指令或内部凭据提取；工具结果侧在进入模型前移除敏感键和指令式数据。Frappe 正式工具信封额外携带 `grounding.schema_version=agent-grounding-v1`、公司、citation 引用和结果集完整性；Orchestrator 同时以权限过滤后的 `model_context/citations/data` 为事实证据，在输出侧确定性校验业务标识符、日期、金额/价格、库存/数量、结果计数、业务状态、公司和“全部/完整”等结论。查询范围和过滤条件与结果事实分开判定：例如“全部日期”不等于“全部结果”，“排除已取消订单”不等于声称某张订单已取消；真正的完整性或状态断言仍必须由工具证据支持。工具结果后的上游 SSE 在完整输出通过校验前不得向客户端发出任何 `message_delta`。首次 Grounding 失败仅允许使用已有 `role=tool` 消息执行一次 `tool_choice=none` 重写，不能调用新工具或新增事实；重写仍失败时以 `AI_AGENT_OUTPUT_GROUNDING_FAILED` 同步/SSE 失败关闭。凭据形态和系统提示泄露继续直接阻断，不进入重写。Guardrail、Grounding 重写、模型决策、工具调用和 Agent Run 使用父子 Span 与持久运行事件记录，稳定失败码通过 HTTP detail 或 SSE `error.code` 返回。

Orchestrator 在输入 Guardrail、包含待执行工具的模型决策、每个正式工具消息和输出 Guardrail 后，通过 Frappe 内部控制面写入 `agent-state-v1`。运行事件写入和检查点读取除 `X-MyApp-AI-Service-Token` 外，还必须携带当前 Run 的 `capability_token`；能力令牌本身不会写入检查点。Frappe 对检查点执行 Run 绑定、大小、字段和敏感键校验。运行事件以 `run_id + event_id` 幂等；遇到明确数据库写冲突或响应丢失时，Orchestrator 只对同一 `event_id` 做有限退避重试，Schema、权限、能力令牌等确定性校验失败不重试。

恢复端点使用与原 Run 相同的请求身份、公司、Prompt 版本、模型别名和工具白名单，并重新校验能力令牌。`model_decision` 检查点会继续执行尚未完成的既有 `tool_calls`；`tool_completed` 从下一模型步骤继续；`output_guardrail` 直接返回已经通过检查的最终内容。SSE 恢复回放已完成输出时，`started/completed` 带 `resumed=true`，回放的 `message_delta/completed` 带 `replayed=true`。Frappe 已通过用户态 `resume_ai_run_v1` 与 `stream_ai_run_resume_v1` 负责所有权校验、失败/过期 Run 重新激活和能力令牌重新签发；浏览器仍不得直接调用 Orchestrator 内部端点。

## 5. SSE

响应类型为 `text/event-stream`。普通 Chat 事件包括 `started`、`message_delta`、`warning`、`completed` 和 `error`；Agent 另外包含 `model_started`、`tool_started`、`tool_completed`，敏感工具暂停时包含 `approval_required`、`paused`。AgentEngine 内部使用 `run_started / output_delta / run_paused / run_completed` 等传输无关事件，HTTP 适配层保持现有 SSE 名称不变。工具结果后的模型步骤使用真实 LiteLLM SSE：若模型继续选择工具，Orchestrator 聚合完整工具参数后发出工具事件；若模型形成最终回答，则逐 delta 转发安全文本。Frappe Gateway 和反向代理必须关闭缓冲并允许长连接。`first_token_ms` 从最终生成请求开始计到首个可见内容 delta；流已开始输出后不跨模型续写。流在部分文本阶段中断时不持久化半段回答，恢复会从最近安全检查点重新生成完整回答。

当前查询 Prompt 版本为 `erp-readonly-v8`。该版本将用户能力描述为“当前账号权限和公司范围内的受控业务查询”，并明确正式写操作必须由用户在业务页面确认；当调用方明示界面已经或将展示结构化明细时，回答不逐条复述记录或重新生成明细清单，只概括查询范围、数量和空结果。若上下文没有声明界面承担明细展示，而用户明确询问金额最高、最近或唯一结果，回答应简要给出与问题直接相关的受控标识、金额和状态。结果覆盖状态不等同于业务健康；没有明确异常字段时不得声称结果正常或无异常。

商品创建/完善 Prompt 版本为 `product-setup-draft-v4`，返回 `operation=auto|create|update`，并可选提取 `standard_selling_rate`、`wholesale_rate`、`retail_rate`、`standard_buying_rate`；`valuation_rate` 仅保留旧响应兼容，不作为新成本字段。所有业务字段只来自用户明确表达；现有商品事实不进入模型猜测，由 Frappe 解析、形成基线并与用户补丁合并。`currency` 仅在用户给出 ISO 4217 代码或完整币种名称时提取，价格后缀“元”或 `¥/￥` 本身返回 `null`。

## 6. 兼容性

增加可选字段属于向后兼容；删除字段、改变枚举、错误码或认证方式需要版本化端点。Prompt 版本是业务兼容边界，不能仅依赖 URL 版本。
