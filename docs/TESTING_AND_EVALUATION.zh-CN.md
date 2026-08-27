# 测试与评测

## 1. 单元测试

```bash
docker build --target test -t myapp-ai:test .
docker run --rm myapp-ai:test
```

当前 test target 包含 Agent Runtime、同步/SSE/恢复规范化轨迹一致性、多工具增量 Function Calling、真实最终 SSE、工具回调、能力令牌、Token 裁剪、Langfuse Agent Span、模型 Tool Calling 探测、策略治理、轨迹评测，以及虚假标识符、金额、库存、状态、公司、完整性声明的一次受控 Grounding 重写与二次失败关闭。数字 Grounding 回归必须同时覆盖结果计数与库存数量的分句隔离，以及销售额、应收应付、实收实付、收付款、回款和到账等常见金额口径；数量随测试目标演进，以 CI 实际结果为准。

## 2. 代码质量

```bash
uv sync --extra test --extra dev --frozen
uv run ruff check .
pre-commit run --all-files
```

CI 还执行依赖审计、运行镜像 HIGH/CRITICAL 漏洞扫描和 CodeQL。

## 3. 独立集成测试

```bash
make integration
```

命令使用仓库内的 `integration.env`，不会读取开发或生产 Secret。合成 Provider 同时模拟 OpenAI Chat/Embedding 和空 Frappe 策略快照。测试验证：

- Orchestrator、Redis、Qdrant 健康。
- Bearer Token Chat 返回确定性合成响应。
- 商品向量 upsert、search、delete 完整闭环。
- 测试结束删除临时 volume，不保留 points。

## 4. 固定评测

`myapp_ai.evals` 内置 40 个纯合成 v1 用例，除自然语言意图、草稿、grounding、Prompt Injection 和写边界外，还验证 Agent 在完整白名单中自主选择 `search_products`、`query_business_documents`、`get_business_report`，以及参数准确性、多工具组合、多轮工具切换、最大调用次数、空结果有限重试、上下文唯一订单精确查询和越权工具拒绝；“带莫字商品”及语义变体，以及“红色可乐饮料”抽取“可乐”核心词、可选的未确认假设、“可乐”不锁品牌和多候选强制澄清均纳入门禁，其中 Agent 多候选澄清属于 critical。候选假设只允许辅助召回排序，不作为必填品牌猜测；critical 用例继续强制查询核心词、返回真实多品牌候选并要求确认。Agent case 默认同时获得三个已注册只读工具，不能根据 expected trajectory 预先缩窄候选；需要验证权限子集时才显式设置 `request.allowed_tools`。`expected_trajectory` 只用于评分，actual trajectory 必须由 `AgentEngine` 的 `run_completed.tool_calls` 生成。多工具独立调用使用 `unordered_contains`，允许模型以不同安全顺序调用，不把 fixture 顺序当作唯一正确轨迹。离线模式通过正式 Function Calling provider replay 和合成 Frappe Tool API 执行完整 Runtime，不访问网络：

```bash
MYAPP_AI_RUNTIME_REVISION=<完整 AI commit> \
python -m myapp_ai.evals.runner --mode offline --output /tmp/myapp-ai-eval-offline.json
```

离线 CLI 使用确定性的评测配置，不要求设置运行时 `MYAPP_AI_SERVICE_TOKEN`、Provider Key 或其他生产 Secret。报告 attempt 使用 `execution_source=provider_replay` 或 `agent_runtime_replay` 区分普通生成和真实 Runtime 回放。

Live 模式必须显式打开计费开关：

```bash
MYAPP_AI_ENABLE_LIVE_EVALS=1 \
MYAPP_AI_RUNTIME_REVISION=<同一完整 AI commit> \
python -m myapp_ai.evals.runner --mode live \
  --model <主模型别名> --model <fallback 别名> \
  --output /tmp/myapp-ai-eval-live.json
```

Agent critical case 在 live 模式使用真实模型 Function Calling，但工具执行固定在无敏感数据的合成 Tool Sandbox，报告标记 `execution_source=live_tool_sandbox`；普通 live case 标记 `live_provider`。staging ERP 端到端评测是独立层，后续报告来源固定为 `staging_erp`，不能与合成 Sandbox 混淆。

报告 Schema 为 `myapp-ai-eval-report-v2`，同时记录完整 Runtime revision、Prompt 版本及内容哈希、工具版本及 Schema 哈希、请求模型别名顺序、数据集版本和 SHA-256。策略治理同时读取 offline/live 两份 full-gate 报告；任一报告过期、两份数据集不一致，或主模型/fallback 未全部执行时失败关闭。评测代码与固定 JSONL 只存在于开发/test 阶段；runtime 镜像会删除 `myapp_ai.evals`，业务请求不读取 replay 或 expected fixture。

结构化意图中的 `confidence` 是模型自报估计值，评测只验证其为 `[0,1]` 范围内的合法数值，不要求精确等于 fixture 中的某个小数；业务意图、实体、日期、状态、排序、金额和数量等可验证字段仍逐项评分。用例可通过 `accepted_json_values` 声明显式等价值，并通过 `unordered_json_paths` 声明本质为集合的字段允许顺序不同；未声明的字段和列表保持精确比较。安全用例可以显式列出允许的 Provider 400/403 硬拒绝码，表示请求在生成任何内容前已被安全边界拒绝；未列入用例的 HTTP、超时和连接错误仍一律失败。报告使用 `PROVIDER_HTTP_<status>`、`PROVIDER_TIMEOUT` 和 `PROVIDER_CONNECTION_ERROR` 等稳定错误码区分拒绝与基础设施故障。

订单与商品草稿的 `operation` 属于必须由固定用例明确断言的业务字段。`evidence` 是可选的提取旁证；未在 fixture 中声明时不参与精确字段评分，声明后仍按完整结构校验，避免文本用例因合法图片/文字旁证扩展被误判，同时保留多模态专用用例对旁证质量的约束能力。

## 5. 发布阈值

- critical、安全、Schema 和禁止模式：100%。
- 结构化字段准确率：至少 95%。
- 普通场景通过率：至少 90%。
- 子集评测只能是 `PARTIAL_PASS`，不能作为发布 gate。
- 报告默认不保存模型原文，只保存哈希、长度、轨迹评分、版本、失败原因、延迟和 Token。

## 6. 本地优先的发布候选流程

staging ERP E2E 用于验证真实策略握手、真实 Frappe 工具信封、持久化 Agent Step、SSE、引用和权限边界，不替代本地测试。为避免按单个模型措辞或分类缺口反复提交、评测和部署，发布按以下顺序执行：

1. 在本地使用失败现场的裁剪工具信封和输出特征复现问题，先增加确定性回归；不得只修改正则后直接部署验证。
2. 对同一事实类型补充语义矩阵，而不是只覆盖单一失败句式。例如数字分类同时覆盖分句、标点、单位省略、结果数量、库存数量和常见收付款措辞。
3. 完成宿主机全量测试、Ruff、pre-commit、Docker test/runtime 和 runtime fixture isolation。
4. 对受影响的真实模型场景执行本地 targeted live 诊断；该结果只能是旁证，不能替代 full gate。
5. 本地稳定后形成单一不可变 release candidate，再为该 revision 执行一次完整 offline/live full gate。中间开发提交不生成 canonical 报告，也不进入 staging。
6. 只有治理校验返回 `release_gate_eligible=true` 后才部署一次 staging，并发布限范围 Policy 执行 ERP E2E。失败必须自动回到 `0%`，现场证据回收至本地后重新开始上述流程。

任何 revision 变化都会使既有治理报告失效。不得拼接 partial 报告、复用旧 revision 报告，或为了减少重跑而降低 Grounding、Schema、安全和工具授权阈值。
