# 性能、容量与 SLO

以下是本地单 Uvicorn、单 Qdrant 的历史基线，不代表生产多副本容量。正式发布必须按目标 Provider 和环境重新校准。

## 1. 单副本合成基线

| 场景 | 最高验证档 | 历史 p95/结果 |
| --- | --- | --- |
| Chat | 100 并发，200/200 | 1122 ms |
| SSE | 200 并发，400/400 | 首 Token 2339 ms，总 2420 ms |
| Structured | 20 并发，40/40 | 300 ms |
| 商品检索 | 提供并发 12 | 60/60，106 ms |
| Embedding | 32/64/128 每批 | 43/55/80 ms |

检索并发 16 曾出现 12.5% 稳定 429，因此初始支持档为 12。429 + `Retry-After` 是有界背压，不应通过无限排队隐藏。

## 2. 初始 SLO

- 被接纳 Chat/SSE/草稿月度成功率 ≥ 99.5%。
- 合成 Chat p95 ≤ 1500 ms；SSE 首 Token p95 ≤ 3000 ms。
- Structured p95 ≤ 500 ms。
- 商品检索在支持容量内 p95 ≤ 250 ms。
- 过载拒绝 100% 使用稳定 429，拒绝 p95 ≤ 500 ms。
- 真实 Provider 暂定 Chat/SSE p95 ≤ 15 秒，必须由 staging ≥100 样本确认。

## 3. 压测

```bash
python scripts/mock_openai_provider.py
python scripts/ai_load_test.py --help
```

压测报告必须记录 commit、镜像 digest、模型别名、Provider、并发、样本、错误分类、p50/p95/p99 和测试日期；不得保存 Secret、原始 ERP 数据或真实模型内容。

## 4. 扩容原则

- 先区分 Orchestrator、Provider、Redis、Qdrant 和网络瓶颈。
- Orchestrator 水平扩容时使用共享 Redis 治理状态。
- 不得只提高 semaphore 绕过 Provider 配额、预算和 Qdrant 容量评审。
- 错误预算耗尽后暂停非紧急模型、Prompt 和 Embedding 发布，保留紧急回滚。
