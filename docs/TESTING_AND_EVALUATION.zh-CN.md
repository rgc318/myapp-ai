# 测试与评测

## 1. 单元测试

```bash
docker build --target test -t myapp-ai:test .
docker run --rm myapp-ai:test
```

当前 test target 包含 80 项，覆盖配置、Prompt、Chat/SSE、结构化草稿、Langfuse、策略、运行时治理、向量客户端、发布校验和评测 runner。

## 2. 代码质量

```bash
uv sync --extra test --extra dev --frozen
uv run ruff check .
pre-commit run --all-files
```

CI 还执行依赖审计、运行镜像 HIGH/CRITICAL 漏洞扫描和 CodeQL。

## 3. 独立集成测试

```bash
cp .env.example .env
make integration
```

合成 Provider 同时模拟 OpenAI Chat/Embedding 和空 Frappe 策略快照。测试验证：

- Orchestrator、Redis、Qdrant 健康。
- Bearer Token Chat 返回确定性合成响应。
- 商品向量 upsert、search、delete 完整闭环。
- 测试结束删除临时 volume，不保留 points。

## 4. 固定评测

`myapp_ai.evals` 内置 21 个纯合成 v1 用例，覆盖三类草稿、grounding、Prompt Injection、禁止正式写操作和敏感信息提取。离线模式不访问网络：

```bash
python -m myapp_ai.evals.runner --mode offline --output /tmp/myapp-ai-eval-offline.json
```

Live 模式必须显式打开计费开关：

```bash
MYAPP_AI_ENABLE_LIVE_EVALS=1 \
python -m myapp_ai.evals.runner --mode live --output /tmp/myapp-ai-eval-live.json
```

## 5. 发布阈值

- critical、安全、Schema 和禁止模式：100%。
- 结构化字段准确率：至少 95%。
- 普通场景通过率：至少 90%。
- 子集评测只能是 `PARTIAL_PASS`，不能作为发布 gate。
- 报告默认不保存模型原文，只保存哈希、长度、版本、失败原因、延迟和 Token。
