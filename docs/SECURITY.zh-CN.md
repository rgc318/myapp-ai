# 安全设计与威胁模型

## 1. 保护对象

- ERP 业务数据、用户身份、公司范围和记录权限。
- 内部服务 Token、LiteLLM Key、Langfuse Project Key。
- Prompt、模型策略、评测证据、向量 alias 和发布审计。
- 模型输入输出、Trace、反馈和质量报告。

## 2. 主要威胁与控制

| 威胁 | 控制 |
| --- | --- |
| 浏览器绕过 Frappe 直接调用 AI | 内部 Token、loopback/内网入口、无 CORS 公网契约 |
| 越权读取 ERP 数据 | 查询只在 Frappe 当前用户上下文执行，AI 不连数据库 |
| Prompt Injection 诱导写单或泄密 | 系统 Prompt、受控工具、只读上下文、Schema 和禁止模式评测 |
| 模型伪造商品/金额/库存 | Frappe 对候选重新查询并二次校验，模型文本不是事实源 |
| Secret 泄漏到日志或 Git | Runtime 注入、忽略规则、密钥扫描、内容默认哈希 |
| 无限并发和费用失控 | 本地 semaphore、Redis 原子 RPM/TPM/预算/并发、熔断 |
| 观测平台拖慢主链路 | 有界非阻塞队列、有限重试、失败开放 |
| Embedding 空间被原地覆盖 | 版本化 collection、full gate、审批、alias 原子切换 |

## 3. 数据分类

- Restricted：Token、Provider Key、用户原始业务请求、可能含个人或交易信息的上下文。
- Confidential：脱敏 Trace、策略、评测报告、向量 payload 元数据。
- Internal：模型别名、Prompt 版本、健康和聚合性能指标。

Restricted 数据不得进入源码、CI Artifact 或默认 Langfuse 内容字段。商品向量文本不得包含价格、库存、订单或客户交易数据。

## 4. 容器与网络

运行镜像使用非 root UID/GID 10001。Compose 设置只读根文件系统、空 capabilities、`no-new-privileges` 和受限 `/tmp`。Qdrant 以 65534 运行；一次性 init 容器只持有 `CHOWN`。Redis/Qdrant 不发布宿主机端口。

## 5. 事件响应

1. 撤销已暴露凭据并生成新版本。
2. 核对 Git、CI、日志、Langfuse 和聊天记录的暴露范围。
3. 同步轮换调用方和接收方，验证旧 Token 401、新 Token 200。
4. 保存不含 Secret 的时间线、指纹、影响和修复证据。
5. 对重复事件增加自动扫描或流程门禁。

公开漏洞报告规则见仓库根目录 `SECURITY.md`。
