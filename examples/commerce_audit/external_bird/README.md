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

## 复现

下载 BIRD 开发集并解压 `financial.sqlite`、`student_club.sqlite` 和 `dev.json` 到同一目录，然后：

```powershell
python examples/commerce_audit/external_bird/scan_bird.py --bird-dir <目录> --output <新文件>
```

脚本拒绝覆盖已有结果，并记录 `dev.json`、契约和守卫源码的 SHA-256。本次结果见 [RESULTS.json](RESULTS.json)。
