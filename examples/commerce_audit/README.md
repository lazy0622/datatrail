# 电商多表分析 Agent：独立审查与实施范围

审查日期：2026-11-07。底座：DB-GPT `9d50d0a55071a040a8cb7684b3b6acfa13cc7c47`。

产品范围、底层架构和技术选型见 [设计说明](../../DESIGN.md)。整体产品能力包括可复用的上游能力；本项目的改动和实验结果另行记录。

2026-11-11 补证据见 [合成扰动、合法对照与模型修复挑战](grain_challenge_v1/README.md)。
52 条合成 SQL 观察记录了复核成本与漏检；8 个真实模型会话中 B/C 最终均正确，
尚未观察到额外最终准确率收益。注入错误 SQL 与无 SQL 的新探针分别报告。

## 独立审查结论

有条件推荐作为 Python / AI 应用开发主项目。项目价值必须由自然模型输出中的真实失败、本人核心改造以及独立任务上的对照结果支持。配置平台、复制口径文档或展示上游报告能力，尚不足以形成主要个人贡献。

预期展示的能力：业务需求拆解、Python 服务与 SQL 调试、模型决策与程序约束的职责划分、第三方系统阅读与修改、错误诊断、实验设计和回归分析。

当前不声称训练/推理引擎能力、生产规模、客户收益、任意 SQL 语义验证或通用 RSI。

## 已核对的事实与边界

- 上游已有 `tools/report_evidence.py`、`report_binding.py`，报告 Agent 已调用证据清单生成。报告溯源整体不是本项目新成果。
- 上游 `examples/olist/olist-analytics-metric-governance.md` 已定义 GMV、AOV、默认 delivered 和预聚合原则。将这些内容放入提示不等于新增算法。
- `examples/dashboard/olist/` 的 gold 查询使用 `olist_` 表名，部分查询统计所有订单；另一示例文档默认 delivered。必须逐题明确合同，不能混用答案。
- `pilot/examples/Olist_Ecommerce.db` 元数据声明 `synthetic_demo`，为人工生成数据，不是真实 Olist 交易全集。
- 对该合成数据库的只读检查发现多明细、多支付订单可产生关联放大。手写 SQL 反例不是模型失败率，更不是改造收益。

## 首轮实施主线

自然语言问题 → 明确指标/时间/状态/维度 → 选择上下文 → SQL → 执行与诊断 → 结果。

先复现真实失败，再选择一项有边界的改造：查询合同、聚合诊断或受控修复。不得预设上游缺少某项能力，也不得为制造收益而削弱基线。

最小对照：

1. A：上游现有自然语言链路。
2. B：同一模型，加足上游已有的治理文档。
3. C：B 加本人新增机制。

保持执行模型和预算可比；分别报告自然任务与构造错误。除正确性外，记录误拦截、退化、额外查询、耗时和 usage。留出任务在改造前划分，私有答案不进入生成/修复上下文。

## 数据核查入口

```powershell
python examples/commerce_audit/preflight.py --output examples/commerce_audit/preflight-result.json
```

只读打开示例数据库，报告数据来源声明、表数量和关联反例。此入口不调用模型，不改变数据，不验证自然语言链路。

公开数据与合成数据共用的关联诊断入口：

```powershell
python examples/commerce_audit/audit_joins.py --db pilot/examples/Olist_Ecommerce.db --output output/commerce-audit/synthetic-joins.json
python examples/commerce_audit/audit_joins.py --db examples/dashboard/olist/data/generated/olist.db --output output/commerce-audit/public-joins.json
```

该诊断将两种 SQL 限定为相同的已交付且有明细和支付记录的订单范围，避免把缺失支付造成的样本差异误说成关联放大。只支持已核验的两种表名模式，不接受任意模型 SQL。公共实现位于 `dbgpt_app/commerce_profile.py`，现已作为 `commerce_profile` 工具接入普通 Agent 工具包；并增加独立的 SQL 模式复核。部署及支持边界见 [DATATRAIL.md](../../DATATRAIL.md)。

针对性回归：

```powershell
python -m pytest examples/commerce_audit/test_audit_joins.py --noconftest -q -o addopts=
```

## 如何展示学习与后续演进

每轮保留一条可复查记录：最初理解 → 小样本推导 → 失败假设 → 代码修改 → 独立复测 → 退化与取舍。本人需能现场解释关键 SQL 和判分逻辑。

有稳定基线后，再考虑从失败反馈提出 Skill/上下文候选修改；必须与人工改进比较，并在封存任务上验证。记忆、反思或自动改提示本身不等于 RSI。
