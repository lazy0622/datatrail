"""Run frozen external Chinook.sqlite evaluation before and after MetricContract."""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import time
from pathlib import Path

import httpx
import sqlglot
from dotenv import load_dotenv

from dbgpt_app.openapi.api_v1.tools.sql_query import make_sql_query
from dbgpt_ext.datasource.rdbms.conn_sqlite import SQLiteConnector

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
DATABASE = ROOT / "output/datatrail/external-v1/Chinook.sqlite"
SPEC = importlib.util.spec_from_file_location(
    "commerce_evaluation_v1", HERE.parent / "evaluation_v1/run_comparison.py"
)
EVALUATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVALUATOR)


async def run_case(
    case: dict, arm: str, frozen: dict, client: httpx.AsyncClient
) -> dict:
    connector = SQLiteConnector.from_file_path(str(DATABASE))
    original = connector.query_ex
    executed, tools, usages = [], [], []

    def query_ex(sql, **kwargs):
        columns, rows = original(sql, **kwargs)
        executed.append(
            {
                "sql": sql,
                "rows": [list(row) for row in (rows or [])[:50]],
                "columns": [str(c) for c in (columns or [])],
                "row_count": len(rows or []),
            }
        )
        return columns, rows

    connector.query_ex = query_ex
    react_state = {}
    if arm == "contract_guard":
        from dbgpt_app.commerce_contract import load_contract

        contract = load_contract("chinook")
        react_state["_commerce_metric_contract"] = contract
        guidance = contract.prompt_summary()
    else:
        guidance = ""
    query = make_sql_query(
        react_state, connector, query_timeout_seconds=15, reuse_results=False
    )
    instructions = (
        "你是 SQLite 数据分析助手。使用 sql_query 查询，不得猜测数值。"
        "按用户指定列顺序返回所有结果行。最终只输出 JSON："
        '{"status":"ok","rows":[[值,...]]}。'
        "SQL 金额按要求保留两位；不得编造字段。\n数据库结构：\n"
        + frozen["schema"]
    )
    if guidance:
        instructions += "\n" + guidance
    messages = [
        {"role": "system", "content": instructions},
        {"role": "user", "content": case["question"]},
    ]
    tool_schema = [
        {
            "type": "function",
            "function": {
                "name": "sql_query",
                "description": (
                    "执行单条只读 SQLite SELECT/WITH 查询，最多15秒；根据反馈修正查询。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"sql": {"type": "string"}},
                    "required": ["sql"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    started = time.monotonic()
    final, failure, responses = None, None, []
    try:
        async with asyncio.timeout(90):
            for _ in range(6):
                response = await client.post(
                    os.environ.get("OPENAI_API_BASE", "https://api.deepseek.com/v1").rstrip("/")
                    + "/chat/completions",
                    headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
                    json={
                        "model": "deepseek-flash",
                        "thinking": {"type": "enabled"},
                        "reasoning_effort": "high",
                        "max_tokens": 4096,
                        "tools": tool_schema,
                        "messages": messages,
                        "stream": False,
                    },
                )
                response.raise_for_status()
                body = response.json()
                usages.append(body.get("usage", {}))
                choice = body["choices"][0]
                message = choice["message"]
                messages.append(message)
                responses.append(
                    {
                        "finish_reason": choice.get("finish_reason"),
                        "reasoning_characters": len(
                            message.get("reasoning_content") or ""
                        ),
                    }
                )
                calls = message.get("tool_calls") or []
                if not calls:
                    final = EVALUATOR.parse_final(message.get("content"))
                    break
                for call in calls:
                    if len(tools) >= 8:
                        raise RuntimeError("tool_budget_exhausted")
                    arguments = json.loads(call["function"]["arguments"])
                    sql = arguments.get("sql")
                    if not isinstance(sql, str):
                        raise ValueError(f"missing_sql_arg:{sorted(arguments.keys())}")
                    payload = json.loads(await query(sql))
                    tools.append({"sql": sql, "error_code": payload.get("error_code")})
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": json.dumps(payload, ensure_ascii=False),
                        }
                    )
            if final is None:
                failure = "round_budget_exhausted"
    except Exception as exc:
        failure = f"{type(exc).__name__}:{exc}"
    finally:
        connector.close()
    final_correct = (
        isinstance(final, dict)
        and final.get("status") == "ok"
        and EVALUATOR.rows_match(final.get("rows"), case["expected_rows"])
    )
    protocol_adherence = None
    if case.get("required_first_sql") and tools:
        try:
            first_ast = sqlglot.parse_one(tools[0]["sql"], read="sqlite")
            req_ast = sqlglot.parse_one(case["required_first_sql"], read="sqlite")
            protocol_adherence = first_ast == req_ast
        except sqlglot.errors.ParseError:
            protocol_adherence = False
    return {
        "case_id": case["id"],
        "split": case["split"],
        "arm": arm,
        "final_correct": final_correct,
        "protocol_adherence": protocol_adherence,
        "gate_rejections": sum(
            t.get("error_code") == "COMMERCE_GRAIN_REVIEW_REQUIRED" for t in tools
        ),
        "executed_count": len(executed),
        "executed_queries": executed,
        "tools": tools,
        "final": final,
        "failure": failure,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "model_calls": len(usages),
        "usage": usages,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--arm", choices=["pre_contract", "contract_guard"], required=True
    )
    args = parser.parse_args()
    load_dotenv(ROOT / ".env.local", override=False)
    os.environ["DATATRAIL_GRAIN_GUARD"] = "1"
    frozen = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
    db_hash = hashlib.sha256(DATABASE.read_bytes()).hexdigest()
    assert db_hash == frozen["database_sha256"]
    out = ROOT / "output/datatrail/external-v1" / args.arm
    out.mkdir(parents=True, exist_ok=True)
    results_file = out / "results.json"
    if results_file.exists():
        raise SystemExit(f"{results_file} already exists; retain prior run")
    results = []
    async with httpx.AsyncClient(timeout=90) as client:
        for case in frozen["cases"]:
            res = await run_case(case, args.arm, frozen, client)
            results.append(res)
            print(
                json.dumps(
                    {
                        "arm": args.arm,
                        "case_id": res["case_id"],
                        "final_correct": res["final_correct"],
                        "gate_rejections": res["gate_rejections"],
                        "executed_count": res["executed_count"],
                        "protocol_adherence": res["protocol_adherence"],
                        "elapsed_seconds": res["elapsed_seconds"],
                    }
                ),
                flush=True,
            )
    summary = {
        "arm": args.arm,
        "database": frozen["database"],
        "database_sha256": frozen["database_sha256"],
        "correct": sum(bool(r["final_correct"]) for r in results),
        "total": len(results),
        "total_gate_rejections": sum(r["gate_rejections"] for r in results),
        "injected_cases_blocked_before_exec": sum(
            r["split"] == "external_injected_fanout" and r["gate_rejections"] > 0
            for r in results
        ),
        "results": results,
    }
    results_file.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("Saved", results_file)


if __name__ == "__main__":
    asyncio.run(main())
