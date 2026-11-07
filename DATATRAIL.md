# DataTrail · 电商经营分析 Agent

基于 [DB-GPT](https://github.com/eosphoros-ai/DB-GPT) 的个人改造项目，固定上游版本为 `9d50d0a55071a040a8cb7684b3b6acfa13cc7c47`。保留原 MIT 许可与版权声明。完整规划见 [PROJECT_OUTLINE.md](PROJECT_OUTLINE.md)。

当前默认模型为 `deepseek-flash`（官方 V4.1-Flash），显式开启 thinking，使用官方默认 High 推理强度；High 是推理设置，不是模型 ID 后缀。此前 V4 Pro 的运行记录保留为历史观察。

## 当前可用能力

- 完整 DB-GPT 后端与前端，使用其数据源、Agent 工具、报告、看板等基础能力。
- `/commerce/` 经营分析工作台：读取 Olist 公开八表数据，展示同范围的关联放大诊断，携带问题及数据源进入原 Agent 页面。
- `commerce_profile` 工具：仅对已选择的 SQLite Olist 数据源执行固定只读诊断，设置快照、查询只读及 15 秒合作式中断。
- `sql_query` 中可切换的粒度复核：识别同一查询层直接关联原始商品和支付明细，并直接 `SUM(别名.price/freight_value/payment_value)` 的模式（含 `SUM(DISTINCT ...)`）；返回结构化反馈与预聚合 / EXISTS 建议，要求模型重写。
- `DATATRAIL_GRAIN_GUARD=0` 可关闭新增工具及复核，保留上游查询行为以供对照。

这不是任意 SQL 的正确性证明。表达式包装、复杂派生列、未经验证的粒度、过滤后的唯一性与有意的金额分摊未由此规则证明；预聚合 CTE 未被拦截也不代表其余口径正确。当前规则会要求部分有意的原始明细聚合重写，须在后续实验中统计误拦截。

## Windows 本地部署

需要 Python 3.11、Node.js（满足 `web/package.json` 及 Next.js 的要求）、Git；本方案不依赖正在运行的 Docker。以下命令从项目根目录执行：

```powershell
py -3.11 -m venv ../.venvs/commerce-analysis
$env:UV_PROJECT_ENVIRONMENT = (Resolve-Path ../.venvs/commerce-analysis).Path
& ../.venvs/commerce-analysis/Scripts/python.exe -m pip install uv==0.8.22
& ../.venvs/commerce-analysis/Scripts/uv.exe sync --package dbgpt-app --no-dev --no-default-groups --locked
& ../.venvs/commerce-analysis/Scripts/python.exe examples/dashboard/olist/initialize_olist_demo.py
Set-Location web
npm ci --no-audit --no-fund
Set-Location ..
Copy-Item configs/datatrail.env.example .env.local
# 在本地编辑 .env.local，填写有效密钥、模型和地址。
./scripts/start-datatrail.ps1
```

访问 `http://127.0.0.1:3000/commerce/`；后端健康检查 `http://127.0.0.1:5670/api/health`。首次进入 Agent 页面需要编译图表依赖。开发构建关闭大体积 source map，并启用 Next.js 的 Webpack 内存优化。

启动脚本默认使用上面的虚拟环境；可传 `-Python <python.exe>` 和 `-Node <node.exe>`。日志及进程 ID 写入 `output/datatrail/`。目前为单机开发部署；上游用户身份默认模拟，服务绑定回环地址，不能直接作为有身份隔离的公网服务开放。

服务主密钥自动生成并保存在 `pilot/meta_data/datatrail.key`，供重启复用。本地 `.env.local`、密钥、数据库、日志、原始数据、运行产物均不应上传 GitHub。

纯 SQL 分析不需要调用 Embedding。启动器在未配置 `EMBEDDING_API_KEY` 时设置 `DBGPT_SKIP_SCHEMA_EMBEDDING=1`，跳过上游启动时的数据库结构向量化，避免无效接口请求；使用直接数据库结构上下文。知识库检索与治理文档入库需要单独配置真实 Embedding 接口。不要把不提供 Embedding 的聊天接口当成向量接口。

## 验证与结果边界

```powershell
& ../.venvs/commerce-analysis/Scripts/uv.exe pip install --python ../.venvs/commerce-analysis/Scripts/python.exe pytest pytest-asyncio ruff
& ../.venvs/commerce-analysis/Scripts/python.exe -m pytest examples/commerce_audit packages/dbgpt-app/src/dbgpt_app/openapi/api_v1/dashboard/tests/test_sql_query_tool.py packages/dbgpt-app/src/dbgpt_app/openapi/api_v1/dashboard/tests/test_sql_security.py --noconftest -q -o addopts=
```

公开数据的七组上游参考查询已复现。相同的 96,477 笔已交付且有商品、支付记录的订单中，商品金额安全汇总为 13,221,363.14 BRL，直接关联支付明细后为 13,813,828.71 BRL，2,875 笔订单发生商品行重复。

这些是数据层反例，不是模型失败率或改造收益。新增测试包含等价金额的有效明细、空结果、缺支付、状态过滤、CTE / EXISTS、安全开关、执行前拒绝、只读哈希一致性、HTTP 接口、超时中断及结构向量化开关。2026-11-07 的相关回归共 99 项通过。

全仓 TypeScript 检查存在上游类型错误，本轮没有将 `ignoreBuildErrors` 当作检查通过。新增工作台单独通过 ESLint；完整类型清理不属于本轮局部改造。

真实 DeepSeek 接口已接通，普通自然语言查询及受控的“错误 JOIN → 复核 → 改写”链路已验证。完整 A/B/C 效果实验仍待实施：保留上游基线、强治理提示基线及新增机制三组，并记录误拦截、正确率、耗时与 usage。自选示例不当作留出集。具体结果与重复调用问题见 [真实模型验证记录](examples/commerce_audit/LIVE_MODEL_SMOKE.md)。

本轮浏览器检查通过桌面工作台、390px 窄屏无横向溢出、进入 Agent 后自动带入问题及数据源。更新本地配置后，真实查询返回 2018 年 7 月商品金额 `867953.46`、订单数 `6159`，与参考 SQL 一致；这是一项成功样例，不是自然任务效果统计。
