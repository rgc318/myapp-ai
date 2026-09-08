# 内部 API 契约

`erp-intent-v8` 的可空 `action_contract` 使用 `ai-action-contract-v1`，保留 execute_request/inquire/negate/clarify、原始 operations、target_count 和 has_preserve_targets，新增 `product_targets[]` 与 `preserve_product_targets[]`（各最多 20 项，每项 query + 当前原文连续 evidence）。商品 enable/disable/delete 仍识别为 `product_setup_draft` 意图，由 Backend 将完整契约路由到独立人工确认计划；模型不能直接执行、生成确认或把 delete/cancel/merge 改成 update。缺少完整目标或当前证据时 Backend 失败关闭。

`query_context_operations` 包含 reset 与 clear_fields。意图生成预算同步/异步均为 4096 completion tokens，以容纳多目标证据；这是输出上限，不是每次固定消耗。Backend/Orchestrator 必须同步升级 v8，旧 Prompt 版本请求仍按标准版本契约拒绝。Schema 测试覆盖目标/保留序列化、空字段、未知确认字段和超量目标。

模型 availability 请求支持 `mode: basic | full`，默认 full 兼容旧调用方。basic 只发最小 Chat/Embedding 请求，不探测工具、结构化或视觉；调用方必须保留已有能力快照，不将 basic 返回的能力默认值解释为“不支持”。full 保持原完整探测。后台队列、持久化、进度和取消归属 Backend，不在 Orchestrator 内保存任务。

## 1. 通用规则

- 除 `GET /livez`、`GET /health` 和 `GET /readyz` 外，所有接口需要 `Authorization: Bearer <MYAPP_AI_SERVICE_TOKEN>`。
- 接口只服务受信任的 Frappe Gateway、后台 Worker 和受控运维工具，不是浏览器公开 API。
- JSON 字段以 Pydantic Schema 为事实源。`ai-runtime-contract-v1` 客户端通过协议和 Schema 能力协商；fresh request 中携带的旧 Prompt revision 不参与兼容判定，由 Orchestrator 选择当前 Prompt。未携带协议的 legacy 请求仍精确匹配 Prompt，Agent resume 也必须精确匹配原 Run 的实际 Prompt revision。
- `401` 表示 Token 错误，`409` 表示协议、Schema 或 legacy/resume Prompt 冲突，`422` 表示请求 Schema，`429` 表示有界背压，`502/503` 表示外部依赖拒绝或暂不可用。
- 受治理 Chat、意图解析或结构化草稿的最终模型尝试被 Provider 拒绝时返回 HTTP 502，`detail.code=MODEL_PROVIDER_REJECTED`，并携带实际 `model_alias` 与可选 `provider_error_code=PROVIDER_HTTP_<status>`；不返回 Provider 原始正文。普通 Chat SSE 使用同字段的 `error` 事件。

## 2. 端点

| 方法与路径                                             | 用途                                                    |
| ------------------------------------------------------ | ------------------------------------------------------- |
| `GET /livez`                                           | 仅检查进程存活，不访问外部依赖                          |
| `GET /health`                                          | 兼容诊断快照、Release、协议及 Manifest 摘要              |
| `GET /readyz`                                          | 非计费 Runtime readiness、Schema family、场景和完整 Manifest；未就绪返回 503 |
| `GET /internal/v1/governance/models`                   | 查询当前 LiteLLM Key 可见的完整模型库存及能力分类       |
| `POST /internal/v1/governance/models/availability`     | 对指定或全部 LiteLLM 可见模型执行最小真实可用性探测     |
| `POST /internal/v1/governance/policy-cache/invalidate` | 使当前进程的 Runtime Policy 缓存立即过期                |
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

### 2.1 Runtime 契约协商

Chat、意图解析、Agent 和四类草稿请求统一接受：

- `protocol_version`：当前为 `ai-runtime-contract-v1`。
- `supported_schema_versions[]`：客户端对当前 Schema family 支持的版本。
- `client_capabilities[]`：当前包括 Release Manifest、响应运行元数据和结构化契约错误能力。
- `prompt_version`：fresh request 可省略；Orchestrator 使用当前 registry revision。仅 legacy 请求和 Agent resume 具有精确匹配语义。

当前 family 为 `chat-v1`、`agent-runtime-v1`、`intent-parse-v1`、`sales-order-draft-v1`、`purchase-order-draft-v1`、`inventory-adjustment-draft-v1`、`product-setup-draft-v1`。不支持的协议返回 `AI_RUNTIME_CONTRACT_MISMATCH`，没有 Schema 交集返回 `AI_SCHEMA_VERSION_MISMATCH`，均为 HTTP 409 且不可重试。

所有主要同步响应以及 SSE 的 `started/completed/paused` 事件返回实际 `protocol_version`、`schema_version`、`prompt_version`、`runtime_revision` 和 `release_id`。调用方必须校验并持久化这些字段；缺失或不兼容时失败关闭，不能把响应当作成功结果。

## 3. Chat 请求最小示例

```json
{
  "messages": [
    {
      "role": "user",
      "content": "继续分析这张图片",
      "attachments": [
        {
          "attachment_id": "AI-ATT-...",
          "mime_type": "image/webp",
          "sha256": "...",
          "width": 1200,
          "height": 800,
          "data_base64": "..."
        }
      ]
    }
  ],
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

没有匹配的已发布 Runtime Policy 时，Orchestrator 会构造 system-default 策略。若请求显式提供 `model_alias`，system-default 的 `model_costs` 仍必须包含该固定模型在 Frappe 快照中的健康、工具、结构化输出和模态元数据；随后才能正确执行固定模型的场景能力校验。不得因为固定模型不在 `MYAPP_AI_MODEL + MYAPP_AI_FALLBACK_MODELS` 中就丢失其能力事实。

Runtime Policy 的模型元数据保留原始 `last_health_status`，并以 Backend 派生的 `effective_health_status` 作为运行选择事实。有效状态语义为：`unavailable` 硬阻断，`degraded / stale / unknown` 允许尝试，`half_open` 使用 Redis `SET NX EX 15` 只允许一个分布式恢复探测。锁冲突返回 HTTP 429、`detail.code=AI_MODEL_HEALTH_HALF_OPEN_BUSY` 和 `Retry-After: 15`；自动链可继续选择后续 fallback。Orchestrator 不根据本地时钟重新计算健康 TTL，避免多服务时区和缓存时刻产生第二套状态事实。

在进入健康、熔断、预算和并发门禁前，Orchestrator 还会按本次运行场景过滤主模型与 fallback：已停用/退役、策略能力不匹配、Agent 工具能力未验证，以及意图/草稿场景结构化输出能力未验证的候选不参与选择。结构化资格使用 `supports_structured_output`，不使用只表示 Provider 原生 strict 能力的 `supports_json_schema`。自动链保持原顺序并提升第一个合格候选，记录 `fallback_reason=primary_model_ineligible_for_scenario`；固定模型不合格返回 HTTP 422 `AI_SELECTED_MODEL_INELIGIBLE`，不会静默换模；自动链没有任何合格候选返回 HTTP 503 `AI_SCENARIO_MODEL_UNAVAILABLE`。staging/production 缺失生命周期或模型元数据同样视为不合格，development/test 可继续使用无治理 system-default 兼容路径。

`context` 只能由服务端加入，内容必须经过权限过滤和字段裁剪。模型文本不能作为商品编码、金额、库存、订单状态或权限判断的事实源。

`POST /internal/v1/intent/parse` 使用 `erp-intent-v7` Prompt 和严格 JSON Schema，返回 `general / product_search / order_query / report_summary / sales_order_draft / purchase_order_draft / inventory_adjustment_draft / product_setup_draft`、置信度、商品核心查询词、明确线索、未确认身份假设、商品属性、单据实体、报表口径、日期、状态、排序、金额下限和数量。商品查询不再把整句机械复制为唯一关键词：`product_query` 保存扩大召回的核心词，`product_terms` 保存名称、品类、颜色、容量、规格、口味和包装等明确线索，`product_attributes` 分字段保存这些线索，`product_hypotheses` 只保存类似“红色可乐可能是可口可乐”的未确认联想。假设只参与候选排序，不能直接成为唯一商品事实。请求可以携带最多 4 张图片；图片商品查询仍优先提取可靠可见的条码、SKU、品牌加商品名或稳定商品名，只有外观类别而没有可靠身份时返回 `product_query=null` 并降低置信度。调用方可在服务端 `context.conversation_state` 中传入裁剪后的 `conversation-state-v2` 工作状态；当前消息优先，状态只用于解析省略和指代。Frappe 仍会在执行边界重新校验公司、权限和真实数据；接口不可用、超时、输出不合法或图片身份未解析时必须失败关闭或回退安全澄清。

结构化意图与四类草稿的 Provider `strict json_schema` 会递归关闭额外字段，并把每个对象的全部属性列入 `required`（可空字段使用 `null` 表达未知），满足 OpenAI Responses 严格 Schema 契约；本地 Pydantic 同样拒绝未声明字段。只有 Provider 明确返回 HTTP 400 表示不支持该 Schema 能力时才降级到 Prompt 内嵌 Schema 的 JSON 模式，HTTP 5xx、超时和连接错误不会伪装成 Schema 兼容回退。

`GET /internal/v1/governance/models` 会读取 LiteLLM `GET /v1/models`，返回当前 Service Key 可见的全部别名。配置的 Embedding 别名或名称包含 `embed / embedding` 的模型分类为 `embedding`，其余当前分类为 `fast_chat`；配置中存在但 LiteLLM 当前不可见的别名返回 `degraded / MODEL_ALIAS_NOT_FOUND`，供 Frappe 同步后阻止继续选择。

模型同步只证明别名对当前 `MYAPP_AI_LITELLM_API_KEY` 可见，不等于模型能够完成实际推理。`POST /internal/v1/governance/models/availability` 对 Chat 模型先发送最小回答请求，再探测结构化输出、强制调用合成 `capability_probe` Function，并执行不在 Prompt 中泄漏答案的红色、蓝色双图片挑战；分别返回 `available`、`supports_json_schema`、`supports_structured_output`、`supports_tools` 与 `supports_vision`。结构化探测优先使用最小 strict JSON Schema；只有 Provider 明确返回 HTTP 400 时才移除 `response_format` 并用 Prompt 内嵌同一 Schema 重试，最终必须解析为精确对象并通过本地校验。原生成功表示两个字段都为 true；兼容回退成功表示 `supports_json_schema=false` 但 `supports_structured_output=true`。两张图片都必须返回精确的小写英文颜色词，任一 Provider 异常或答案不匹配都会把本次 `supports_vision` 重置为 false。Embedding 模型发送一条固定合成文本。响应只保留能力、耗时、Provider 模型名和稳定错误码，不保存模型输出或 Provider 错误原文。该操作会产生少量真实 Provider 调用和费用。

Frappe 应先按注册表、人工状态和调用权限解析检测范围，再向 Orchestrator 发送明确 alias 列表：

```json
{
  "model_aliases": ["gpt-5.5", "opencode-deepseek-v4-flash"]
}
```

直接传空列表表示检查当前 LiteLLM Key 可见的全部模型；浏览器不得绕过 Frappe 直接使用这一语义。Orchestrator 会去重 alias，拒绝空字符串或超过 140 字符的值，单次最多 100 个。

`POST /internal/v1/governance/policy-cache/invalidate` 只接受内部 Service Token。Frappe 在模型健康状态和审计提交后调用该端点；端点只把缓存标记为过期，不删除最后一个已验证快照。下一次请求会重新向 Frappe 取快照，若 Frappe 暂时不可达仍可按既有失败关闭/最后已验证快照规则处理。普通 Chat、SSE 和结构化草稿若发现固定模型为缓存中的新鲜 `unavailable`，或自动链全部候选均为新鲜 `unavailable`，也会先强制刷新一次再交给 Runtime Guard，避免失效通知丢失造成持续误阻断；过期失败由 Backend 派生为 `half_open`，不再命中永久阻断判断。

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
      "supports_json_schema": false,
      "supports_structured_output": true,
      "structured_error_code": null,
      "supports_vision": true,
      "vision_error_code": null,
      "latency_ms": 1580,
      "provider_model": "gpt-5.5",
      "error_code": null
    },
    {
      "model_alias": "opencode-deepseek-v4-flash",
      "capability": "fast_chat",
      "available": false,
      "supports_tools": false,
      "supports_json_schema": false,
      "supports_structured_output": false,
      "structured_error_code": null,
      "supports_vision": false,
      "vision_error_code": null,
      "latency_ms": 8708,
      "provider_model": null,
      "error_code": "PROVIDER_HTTP_403"
    }
  ]
}
```

稳定错误码至少区分 Provider HTTP 拒绝、超时、网络错误、空响应、工具调用未返回、结构化输出为空、JSON 非法、Schema 不匹配和模型别名不存在。错误码用于治理、趋势和排障，不携带 Provider 原始响应正文。一次探测成功或失败都只是当时快照，不允许 Orchestrator 自动改变 Frappe 中的人工生命周期状态或发布策略。

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

模型返回的 `tool_calls` 必须命中本 Run 白名单，并通过与 Function Calling 定义相同的严格参数 Schema；额外字段、类型漂移、越界数值、数组最少/最多项和非法枚举均在调用 Frappe 前阻断。`query_business_documents` 当前工具版本为 `v2`，参数包含至少一个 `entities` 和必填可空 `document_name`；非空单据号只能搭配一个单据类型。Orchestrator 把 Frappe 结构化结果作为正式 `role=tool` 消息回传模型，并限制最大模型步骤、工具调用次数、统一 Run deadline、累计 Token、工具超时与上下文 Token 预算。staging/production 只允许模型注册表中 `supports_tools=true` 的已验证模型进入 Agent 路径。

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

当前查询 Prompt 版本为 `erp-readonly-v11`。Agent 请求中的普通业务上下文继续失败关闭，只允许把白名单裁剪后的 `conversation-state-v2` typed entity references 放入 `<conversation_state>`；它们只能用于消解省略和生成精确工具参数，不能作为实时业务事实。商品工具 v2 接收核心词、查询变体、未确认假设和属性线索，并执行关键词与语义混合召回。工具返回 `clarification.required=true` 时，模型必须把结果表述为待确认候选：单候选询问用户是否正确，多候选请用户结合品牌、规格、口味或包装选择；不得擅自选择第一条或声称“唯一匹配”。工具成功后最终回答必须实际消费受控结果，不能退回通用欢迎语；商品返回条数与库存数量严格分离，单据摘要必须明确单据类型、状态筛选和返回数量。其他结构化明细仍由界面展示，模型只做简短摘要；结果覆盖状态不等同于业务健康。

当前草稿 Prompt 版本为 `sales-order-draft-v5`、`purchase-order-draft-v5`、`inventory-adjustment-draft-v3`、`product-setup-draft-v7`。商品使用 `target + patch` 分离旧实体身份与新值；订单使用 `target + header_patch + line_update_mode + line_changes`，默认保留未提及行，只有明确 `replace_all` 才整单替换；库存语义不足时 `adjustment_type=null`。每条 `ChatMessage.attachments` 最多 4 个，每项包含 `attachment_id / filename / mime_type / sha256 / width / height / data_base64`；Orchestrator 校验 base64、8MB 规范化后上限和 SHA-256，并在每个 OpenAI `image_url` 内容块前加入服务端生成的 `<image_attachment attachment_id="..." />` 文本标记，使结构化结果可以在 `evidence[].attachment_id` 中精确引用来源图片。

v5 订单命令的 `target.order_number/context_ref` 只定位原订单，`header_patch` 只保存明确表头修改，`header_patch.clear_fields` 专门表达清空销售备注、采购备注或供应商参考号，`line_changes[].target` 定位原行，`line_changes[].patch` 保存新值。`replace_all` 和新建订单只允许 add 行；替换商品或单位时 Backend 会重新解析计价基础。v7 商品命令同样禁止从 patch 反推 target。滚动迁移期 Schema 仍接受 v4 平铺字段，但 Prompt 不再正常输出这些兼容字段。

图片请求只允许策略中 `supports_vision=true` 的模型。固定模型不支持图片返回 HTTP 422 / `AI_SELECTED_MODEL_NO_VISION`；策略没有经过验证的视觉候选返回 HTTP 503 / `AI_VISION_MODEL_REQUIRED`。

## 6. 兼容性

增加可选字段通常向后兼容；删除字段、改变枚举、错误码或认证方式需要新的协议或 Schema family 版本。Prompt revision 是运行实现与审计事实，不再是 fresh request 的跨服务兼容边界；Agent checkpoint 恢复仍以原 Prompt revision 为安全边界。
# 商品价格提取增量（2026-09-09）

商品 Prompt 升级 `product-setup-draft-v8`，Backend runtime 期望版本必须同步。`ProductSetupPatchCandidate` 新增 nullable `prices[]`（最多40行）、nullable `uom_relations[]`（最多20条）、`pricing_unresolved[]`（最多10条）。每行价格保留用途、金额、单位、币种、explicit/inferred 和原文 evidence；每条数量关系表达 from_qty/from_uom = to_qty/to_uom，未知数量返回 null，禁止按售价比例推算。新生成使用明细，不用旧四个价格标量替代。未支持阶梯、有效期等条件必须进入 pricing_unresolved，不得静默丢弃。Standard Selling 同单位批发默认策略由 Backend 实施，模型不必重复生成。商品提取 completion 预算4096。真实业务校验/单位换算/确认执行仍由 Backend 负责。
