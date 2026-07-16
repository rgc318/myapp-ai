# 向量检索与 Embedding 发布

## 1. 数据边界

商品向量文本只允许商品编码、名称、昵称、规格、用途、品牌、分类、条码和单位。禁止价格、库存、订单、客户、供应商和其他交易数据。Qdrant 返回候选后，Frappe 必须重新应用用户权限和实时业务过滤。

## 2. Collection 与 Alias

- 物理 collection 名称必须版本化，例如 `myapp-products-v2`。
- 在线读写使用稳定 `MYAPP_AI_QDRANT_ALIAS`，例如 `myapp-products-live`。
- 模型、维度或向量空间变化必须新建 collection，不能原地覆盖。
- 旧 collection 保留到观察和回滚窗口结束。

## 3. 发布门禁

1. 注册不可变 Embedding 能力别名并验证单条/批量维度。
2. 构建候选 collection，确保排除测试前缀和目标商品点数一致。
3. 运行版本化语义质量集、权限二次过滤、删除幂等、删除后恢复和延迟门禁。
4. 把脱敏 full-gate 报告只读挂载到受控路径。
5. 起草、审批并由授权管理员原子切换 alias。
6. 观察错误率、Top-K 和延迟；异常时把 alias 指回旧 collection。

浏览器上传的报告不能直接成为发布证据；Orchestrator 会重新检查 Schema、模型、collection、full gate 和阈值。

## 4. 运行验证

```bash
python -m myapp_ai.retrieval_quality --output /tmp/product-retrieval.json
```

真实 Provider 评测必须显式设置 `MYAPP_AI_ENABLE_LIVE_EVALS=1`。Provider 错误、排除候选泄漏或阈值失败时命令返回非零。

当前历史 v1 基线为 143 points、1024 维，30 条中文门禁 Top-1 96.67%、Top-3 100%、Provider error 0、排除泄漏 0；这是特定日期和向量空间证据，不能自动代表后续模型。

## 5. 备份恢复

使用 Qdrant snapshot API 或 `scripts/qdrant_snapshot.py`，不要直接复制运行中的 RocksDB 目录。恢复到隔离 collection 后核对 point 数、维度、payload 和质量集，再决定 alias 切换。
