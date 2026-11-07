"""Replay a fixed successful business query; this does not call an LLM."""

import argparse
import asyncio
import json
from pathlib import Path

from dbgpt_app.openapi.api_v1.tools.sql_query import make_sql_query
from dbgpt_ext.datasource.rdbms.conn_sqlite import SQLiteConnector

SQL = """SELECT COUNT(DISTINCT o.order_id) AS orders,
ROUND(SUM(i.price), 2) AS gmv
FROM olist_orders o JOIN olist_order_items i ON i.order_id=o.order_id
WHERE o.order_status='delivered'
AND o.order_purchase_timestamp >= '2018-07-01'
AND o.order_purchase_timestamp < '2018-08-01'"""


async def replay(path: Path, repeats: int, reuse: bool) -> dict:
    connector = SQLiteConnector.from_file_path(str(path))
    original = connector.query_ex
    executions = 0

    def counted_query(*args, **kwargs):
        nonlocal executions
        executions += 1
        return original(*args, **kwargs)

    connector.query_ex = counted_query
    query = make_sql_query({}, connector, reuse_results=reuse)
    try:
        results = [json.loads(await query(SQL)) for _ in range(repeats)]
    finally:
        connector.close()
    if any(result.get("success") is False for result in results):
        raise RuntimeError("Replay query failed")
    return {
        "reuse_enabled": reuse,
        "tool_submissions": repeats,
        "database_executions": executions,
        "reused_results": sum(bool(r.get("result_reused")) for r in results),
        "result_chunks_identical": all(
            r["chunks"][0] == results[0]["chunks"][0] for r in results
        ),
        "first_result": results[0]["chunks"][0],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 2 <= args.repeats <= 20:
        parser.error("--repeats must be between 2 and 20")
    path = args.db.resolve(strict=True)
    if args.output and args.output.resolve() == path:
        parser.error("The output must not overwrite the database")
    report = {
        "evaluation_tier": "controlled_replay_not_model_performance",
        "sql": SQL,
        "baseline": asyncio.run(replay(path, args.repeats, False)),
        "reuse": asyncio.run(replay(path, args.repeats, True)),
    }
    report["expectations_satisfied"] = (
        report["baseline"]["database_executions"] == args.repeats
        and report["reuse"]["database_executions"] == 1
        and report["reuse"]["reused_results"] == args.repeats - 1
        and report["baseline"]["first_result"] == report["reuse"]["first_result"]
        and report["reuse"]["result_chunks_identical"]
    )
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    if not report["expectations_satisfied"]:
        raise SystemExit("Query reuse replay did not meet its acceptance criteria")


if __name__ == "__main__":
    main()
