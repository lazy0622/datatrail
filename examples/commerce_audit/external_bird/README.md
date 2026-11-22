# BIRD 外部标注 SQL 扫描

日期：2026-11-21。不调用模型。目的是在别人写的、带人工标注的 SQL 上测量粒度检查的误报。之前的题都是作者自己出的，这组数据回答的是“守卫在外部数据上会不会乱拦”。

## 数据

[BIRD](https://bird-bench.github.io/) 开发集 `dev_20240627` 中的两个库：

| 库 | 题数 | 一对多结构（脚本在数据上重新核对） |
| --- | ---: | --- |
| `financial` | 106 | 4,500 个账户每个都有多条 `trans`；1,655 个有多条 `order`；869 个有两条 `disp`；每个账户最多一笔 `loan`，恰好一个 `OWNER` |
| `student_club` | 158 | 7 个 `budget` 有多条 `expense`；17 个活动有多条 `attendance`；3 个成员有多条 `income` 或 `expense` |

契约 [`bird_financial.json`](../../../configs/metric_contracts/bird_financial.json) 和 [`bird_student_club.json`](../../../configs/metric_contracts/bird_student_club.json) 只根据上表的表结构和基数编写，编写时没有对照标准 SQL 调规则。BIRD 数据库不随仓库提交。

## 结果

| 阶段 | financial 复核 | student_club 复核 |
| --- | ---: | ---: |
| 首次扫描（契约不含 `one_row_filters`） | 2/106 | 0/158 |
| 契约支持 `one_row_filters` 后 | 1/106 | 0/158 |

首次扫描的结果没有单独保存。去掉契约里的 `one_row_filters` 字段重跑，可以复现 #169 和 #179 两条复核。

两次复核都是误报，标准 SQL 本身没有重复计算：

- **#169**：经 `disp` 关联客户后累计贷款金额，但条件里有 `disp.type = 'OWNER'`，每个账户恰好一个所有人，不会重复。守卫原来不认识这种“过滤后变成一对一”的写法。契约新增 `one_row_filters` 后，声明 `disp.type = 'OWNER'` 是一对一过滤，这条不再复核。同一机制也消除了 Olist 合法对照 L06（`payment_sequential = 1`）的 4 条误报。
- **#179**：按单个客户累计交易金额，需要经过 `disp`。这个客户只有一条 `disp`，所以不重复。这条唯一性来自数据本身，SQL 里看不出来，契约也无法声明，所以仍然复核。改写为子查询后结果相同（303,276），见 [`adjudication.json`](adjudication.json)。

264 道外部题中最终复核 1 道，误报率约 0.4%。

## 能说明什么

- 在外部人工 SQL 上，守卫很少打扰正确查询。
- BIRD 标准 SQL 里没有出现这类重复计算错误，所以这组数据测的是误报，测不到守卫拦截的价值。拦截价值仍以 Chinook 注入实验和 Olist 扰动实验为准。
- 判定由作者人工完成，不是 BIRD 官方标注。未被复核的题也不代表 SQL 正确。只检查 `SUM`；`AVG` 在重复行上的偏差不在检查范围内。

## 模型自己写 SQL（2026-11-21）

上面的扫描只能测误报。为了回答“守卫在模型真实输出上有没有用”，让 DeepSeek（`deepseek-flash`，思考模式，推理强度 high）为同样 264 道题各写一条 SQL，脚本 [`run_model_bird.py`](run_model_bird.py)，结果 [`MODEL_RESULTS.json`](MODEL_RESULTS.json)。

配对设计：每题只采样一次。不加守卫时直接执行这条 SQL。加守卫时先检查同一条 SQL，被拦下的把守卫提示发回模型，最多改写两次。正确性按 BIRD 执行准确率判定，即结果行集合与标准 SQL 相同。契约和守卫在运行前冻结。

| | financial（106） | student_club（158） | 合计（264） |
| --- | ---: | ---: | ---: |
| 不加守卫，执行正确 | 60 | 103 | 163（61.7%） |
| 加守卫，执行正确 | 59 | 103 | 162 |
| 守卫拦截 | 1 | 0 | 1 |

唯一一次拦截是 #132，而且拦错了。这次运行的守卫临时加了 `AVG` 检查。模型原本的 SQL 是对的；守卫提示里写着“若只关心账户所有人，请加 `disp.type = 'OWNER'`”，模型照做后改变了统计对象，答案变错。

101 道答错的题里，有 17 道在关联查询上用了 `SUM` 或 `AVG`。逐条看过，没有一道是重复累加造成的，错误来自理解偏差、日期解析、选错表和列格式，见 [`model_error_review.json`](model_error_review.json)。模型还多次自己用 `EXISTS` 或 `DISTINCT` 去重。

结论：

- 在 BIRD 这两个库上，这个模型几乎不犯重复累加错误，守卫在自然任务上没有提高准确率。它的价值仍限于 Chinook 注入和 Olist 扰动这类已知陷阱，以及作为执行前的保险。
- 守卫的提示会改变模型的答案。提示里给出的具体改法（加 `OWNER`）会被模型当成指令执行，所以提示应当只要求去重，不应建议改变统计口径。这条还没有改，修改后需要另找数据验证。
- 当前 SQL 准确率的瓶颈是理解题意和选字段，不在聚合粒度。要提高准确率，应该做字段说明和取值示例，而不是继续加粒度规则。

### AVG 检查已撤回

加 `AVG` 后，标准 SQL 扫描的复核从 1 道增加到 3 道（新增 #132、#144，都是误报：在这份数据里没有重复行，去重改写结果相同，见 [`RESULTS-v2-avg.json`](RESULTS-v2-avg.json)），模型 SQL 上没有拦对任何一题，还造成 #132 一次答错。因此撤回 `AVG` 检查，守卫仍只检查 `SUM`。用撤回后的守卫重新检查 264 条模型 SQL，拦截 0 条，即加不加守卫结果相同（163/264）。

## 复现

下载 BIRD 开发集并解压 `financial.sqlite`、`student_club.sqlite` 和 `dev.json` 到同一目录，然后：

```powershell
python examples/commerce_audit/external_bird/scan_bird.py --bird-dir <目录> --output <新文件>
python examples/commerce_audit/external_bird/run_model_bird.py --bird-dir <目录> --output <新文件>
```

脚本拒绝覆盖已有结果，并记录 `dev.json`、契约和守卫源码的 SHA-256。本次结果见 [RESULTS.json](RESULTS.json)。第二个脚本会调用模型，264 道题约 32 万 token。

## 上下文增强：开发集提升，保留集无效（2026-11-22）

假设：模型答错多是因为不清楚列的含义和取值格式。`dbgpt_app/schema_context.py` 在建表语句之外补三类信息：BIRD 提供的列说明、每列最常见的取值、问题里的词在库中精确匹配到的值。脚本 [`run_context_eval.py`](run_context_eval.py)，每题每组各采样一次。

为避免拿看过的错题调方案，开发集只用 financial 和 student_club；保留集用 superhero、california_schools、toxicology、debit_card_specializing 四个此前没打开过的库。方案在开发集跑完后提交冻结（`6fbb856`），再跑保留集。事先定的放弃标准：保留集提升不到 3 个百分点就不采用。

| | 只给建表语句 | 加上下文 | 改对 / 改错 |
| --- | ---: | ---: | ---: |
| 开发集（264） | 166（62.9%） | 177（67.1%） | 21 / 10 |
| 保留集（427） | 284（66.5%） | 284（66.5%） | 26 / 26 |

保留集上没有提升。改对和改错的题一样多，分布在各个库，说明加的上下文主要是让答案换了一批，并没有系统性地纠正错误。开发集的 +4.2 个百分点本身也不显著（符号检验 p≈0.07），很可能来自随机波动，加上设计时参考过这两个库。提示 token 增加约 3.5 倍。

结论：对这个模型，在已经提供 BIRD 外部知识的情况下，列说明和取值示例不能提高准确率。方案不接入 DataTrail，模块和结果只作为实验记录保留。结果见 [`context_runs/`](context_runs/)。
