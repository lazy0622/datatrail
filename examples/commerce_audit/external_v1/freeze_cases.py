"""Freeze external Chinook.sqlite evaluation cases with verified SQLite ground truth."""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
DATABASE = ROOT / "output/datatrail/external-v1/Chinook.sqlite"

CASES = [
    {
        "id": "C01",
        "split": "external_natural",
        "question": (
            "统计 2024 年（[2024-01-01, 2025-01-01)）的发票总金额和发票数量。"
            "结果每行依次为[总金额, 发票数]，金额保留两位。"
        ),
        "reference_sql": (
            "SELECT ROUND(SUM(Total), 2), COUNT(*) FROM Invoice "
            "WHERE InvoiceDate >= '2024-01-01' AND InvoiceDate < '2025-01-01'"
        ),
    },
    {
        "id": "C02",
        "split": "external_natural",
        "question": (
            "统计 2024 年（[2024-01-01, 2025-01-01)）按账单国家(BillingCountry)汇总的"
            "发票总金额前 3 名，"
            "按总金额降序、国家名升序排列。每行依次为[国家, 总金额]，金额保留两位。"
        ),
        "reference_sql": (
            "SELECT BillingCountry, ROUND(SUM(Total), 2) AS s FROM Invoice "
            "WHERE InvoiceDate >= '2024-01-01' AND InvoiceDate < '2025-01-01' "
            "GROUP BY BillingCountry ORDER BY s DESC, BillingCountry ASC LIMIT 3"
        ),
    },
    {
        "id": "C03",
        "split": "external_natural",
        "question": (
            "统计全量历史数据中 Genre.Name='Rock' 曲目的明细销售总金额"
            "（UnitPrice * Quantity）"
            "和涉及的发票去重数。每行依次为[总金额, 发票数]，金额保留两位。"
        ),
        "reference_sql": (
            "SELECT ROUND(SUM(il.UnitPrice * il.Quantity), 2), "
            "COUNT(DISTINCT il.InvoiceId) "
            "FROM InvoiceLine il JOIN Track t ON t.TrackId = il.TrackId "
            "JOIN Genre g ON g.GenreId = t.GenreId WHERE g.Name = 'Rock'"
        ),
    },
    {
        "id": "C04",
        "split": "external_injected_fanout",
        "question": (
            "统计 2024 年（[2024-01-01, 2025-01-01)）发票总金额和涉及发票去重数。"
            "每行依次为[总金额, 发票数]，金额保留两位。\n"
            "这是给定候选查询的受控修复流程。第一次 sql_query 调用"
            "必须按原样执行下面候选 SQL，"
            "然后根据工具反馈或数据库核对完成准确统计：\n"
            "候选 SQL：\n"
            "SELECT ROUND(SUM(i.Total), 2), COUNT(DISTINCT i.InvoiceId) "
            "FROM Invoice i JOIN InvoiceLine il ON il.InvoiceId = i.InvoiceId "
            "WHERE i.InvoiceDate >= '2024-01-01' AND i.InvoiceDate < '2025-01-01'"
        ),
        "required_first_sql": (
            "SELECT ROUND(SUM(i.Total), 2), COUNT(DISTINCT i.InvoiceId) "
            "FROM Invoice i JOIN InvoiceLine il ON il.InvoiceId = i.InvoiceId "
            "WHERE i.InvoiceDate >= '2024-01-01' AND i.InvoiceDate < '2025-01-01'"
        ),
        "reference_sql": (
            "SELECT ROUND(SUM(Total), 2), COUNT(*) FROM Invoice "
            "WHERE InvoiceDate >= '2024-01-01' AND InvoiceDate < '2025-01-01'"
        ),
    },
    {
        "id": "C05",
        "split": "external_injected_fanout",
        "question": (
            "统计全量历史数据中曲目流派 GenreId=1 且在播放列表中有记录的"
            "明细销售总金额（UnitPrice * Quantity）。"
            "只返回一行一列[总金额]，金额保留两位。\n"
            "这是给定候选查询的受控修复流程。第一次 sql_query 调用"
            "必须按原样执行下面候选 SQL，"
            "然后根据工具反馈或数据库核对完成准确统计：\n"
            "候选 SQL：\n"
            "SELECT ROUND(SUM(il.UnitPrice * il.Quantity), 2) "
            "FROM InvoiceLine il JOIN Track t ON t.TrackId = il.TrackId "
            "JOIN PlaylistTrack pt ON pt.TrackId = t.TrackId WHERE t.GenreId = 1"
        ),
        "required_first_sql": (
            "SELECT ROUND(SUM(il.UnitPrice * il.Quantity), 2) "
            "FROM InvoiceLine il JOIN Track t ON t.TrackId = il.TrackId "
            "JOIN PlaylistTrack pt ON pt.TrackId = t.TrackId WHERE t.GenreId = 1"
        ),
        "reference_sql": (
            "SELECT ROUND(SUM(il.UnitPrice * il.Quantity), 2) "
            "FROM InvoiceLine il JOIN Track t ON t.TrackId = il.TrackId "
            "WHERE t.GenreId = 1 "
            "AND EXISTS (SELECT 1 FROM PlaylistTrack pt WHERE pt.TrackId = t.TrackId)"
        ),
    },
    {
        "id": "C06",
        "split": "external_natural",
        "question": (
            "统计 2024 年（[2024-01-01, 2025-01-01)）在播放列表(PlaylistTrack)中"
            "至少出现过一次的 Rock 曲目的实际明细销售总金额"
            "（不得因多播放列表关联重复计算）和涉及的发票去重数。"
            "每行依次为[总金额, 发票数]，金额保留两位。"
        ),
        "reference_sql": (
            "SELECT ROUND(SUM(il.UnitPrice * il.Quantity), 2), "
            "COUNT(DISTINCT il.InvoiceId) "
            "FROM Invoice i JOIN InvoiceLine il ON il.InvoiceId = i.InvoiceId "
            "JOIN Track t ON t.TrackId = il.TrackId "
            "JOIN Genre g ON g.GenreId = t.GenreId "
            "WHERE g.Name = 'Rock' AND i.InvoiceDate >= '2024-01-01' "
            "AND i.InvoiceDate < '2025-01-01' "
            "AND EXISTS (SELECT 1 FROM PlaylistTrack pt WHERE pt.TrackId = t.TrackId)"
        ),
    },
]


def main() -> None:
    out = HERE / "cases.json"
    if out.exists():
        raise SystemExit("cases.json exists; never overwrite frozen cases")
    with closing(sqlite3.connect(DATABASE.as_uri() + "?mode=ro", uri=True)) as conn:
        schema_rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        schema = "\n".join(row[0] for row in schema_rows if row[0])
        cases = []
        for item in CASES:
            expected_rows = [
                list(r) for r in conn.execute(item["reference_sql"]).fetchall()
            ]
            cases.append({**item, "expected_rows": expected_rows})
    payload = {
        "version": 1,
        "database": "Chinook.sqlite",
        "database_sha256": hashlib.sha256(DATABASE.read_bytes()).hexdigest(),
        "schema": schema,
        "notes": [
            "External SQLite schema (Chinook) distinct from Olist ecommerce schema.",
            "C04/C05 are controlled fan-out probes comparing before/after contract.",
            "C01/C02/C03/C06 are natural queries on Chinook.",
        ],
        "cases": cases,
    }
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("Froze", len(cases), "Chinook cases into", out)


if __name__ == "__main__":
    main()
