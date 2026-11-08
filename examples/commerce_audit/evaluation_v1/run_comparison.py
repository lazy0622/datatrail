"""Controlled SQL-tool A/B/C evaluation, not the complete UI ReAct agent."""

import argparse
import asyncio
import hashlib
import json
import math
import os
import sqlite3
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

from dbgpt_app.openapi.api_v1.tools.sql_query import make_sql_query
from dbgpt_ext.datasource.rdbms.conn_sqlite import SQLiteConnector

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
DATABASE = ROOT / "examples/dashboard/olist/data/generated/olist.db"
GOVERNANCE = ROOT / "examples/olist/olist-analytics-metric-governance.md"
GATE = ROOT / "packages/dbgpt-app/src/dbgpt_app/commerce_grain.py"


def rows_match(actual, expected):
    if not isinstance(actual, (list, tuple)) or len(actual) != len(expected):
        return False
    for left, right in zip(actual, expected):
        if not isinstance(left, (list, tuple)) or len(left) != len(right):
            return False
        for value, target in zip(left, right):
            if isinstance(value, (int, float)) and isinstance(target, (int, float)):
                if not math.isclose(value, target, rel_tol=0, abs_tol=0.005):
                    return False
            elif value != target:
                return False
    return True


def parse_final(content):
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


async def one_case(case, arm, frozen, client):
    connector = SQLiteConnector.from_file_path(str(DATABASE))
    original = connector.query_ex
    executed, tools, usages = [], [], []

    def query_ex(sql, **kwargs):
        columns, rows = original(sql, **kwargs)
        executed.append(
            {
                "sql": sql,
                "rows": (rows or [])[:50],
                "columns": columns,
                "row_count": len(rows or []),
            }
        )
        return columns, rows

    connector.query_ex = query_ex
    query = make_sql_query({}, connector, query_timeout_seconds=15, reuse_results=False)
    instructions = (
        "你是 SQLite 电商数据分析助手。用 sql_query 查询，不猜数值。"
        "按用户指定的列顺序返回所有结果行。最终只输出 JSON："
        '{"status":"ok","rows":[[值,...]]}。'
        '数据不支持的问题返回 {"status":"unsupported","rows":[]}。'
        "SQL金额按要求保留两位；没有依据时不得编造业务字段。\n数据库结构：\n"
        + frozen["schema"]
    )
    if arm in {"B", "C"}:
        instructions += "\n完整业务治理文档：\n" + GOVERNANCE.read_text(
            encoding="utf-8"
        )
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
                    os.environ.get(
                        "OPENAI_API_BASE", "https://api.deepseek.com/v1"
                    ).rstrip("/")
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
                # Preserve provider reasoning_content for tool continuations,
                # as required by DeepSeek; do not publish the reasoning text.
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
                    final = parse_final(message.get("content"))
                    break
                for call in calls:
                    if len(tools) >= 8:
                        raise RuntimeError("tool_budget_exhausted")
                    if call["function"]["name"] != "sql_query":
                        raise ValueError("unknown_tool")
                    arguments = json.loads(call["function"]["arguments"])
                    sql = arguments["sql"]
                    payload = json.loads(await query(sql))
                    entry = {"sql": sql, "error_code": payload.get("error_code")}
                    if payload.get("grain_review"):
                        # A rejected SELECT can be replayed only in this
                        # independent, read-only oracle to measure overblocking.
                        try:
                            with sqlite3.connect(
                                DATABASE.as_uri() + "?mode=ro", uri=True
                            ) as connection:
                                deadline = time.monotonic() + 15
                                connection.set_progress_handler(
                                    lambda: int(time.monotonic() > deadline), 10000
                                )
                                entry["rejected_rows_match_reference"] = rows_match(
                                    connection.execute(sql).fetchall(),
                                    case["expected_rows"],
                                )
                        except Exception:
                            entry["rejected_rows_match_reference"] = None
                    tools.append(entry)
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
        failure = type(exc).__name__ + (
            ":" + str(exc) if isinstance(exc, RuntimeError) else ""
        )
    finally:
        connector.close()
    unsupported = bool(case.get("unsupported"))
    final_correct = isinstance(final, dict) and (
        final.get("status") == "unsupported"
        if unsupported
        else final.get("status") == "ok"
        and rows_match(final.get("rows"), case["expected_rows"])
    )
    return {
        "case_id": case["id"],
        "split": case["split"],
        "arm": arm,
        "completed": final is not None,
        "final_correct": final_correct,
        "last_sql_correct": None
        if unsupported
        else bool(executed and rows_match(executed[-1]["rows"], case["expected_rows"])),
        "final": final,
        "failure": failure,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "model_calls": len(usages),
        "tools": tools,
        "executed_queries": executed,
        "responses": responses,
        "usage": usages,
    }


async def run(arm):
    load_dotenv(ROOT / ".env.local", override=False)
    os.environ["DATATRAIL_GRAIN_GUARD"] = "1" if arm == "C" else "0"
    case_bytes = (HERE / "cases.json").read_bytes()
    frozen = json.loads(case_bytes)
    assert (
        hashlib.sha256(DATABASE.read_bytes()).hexdigest() == frozen["database_sha256"]
    )
    folder = ROOT / "output/datatrail/evaluation-v1" / arm
    folder.mkdir(parents=True, exist_ok=True)
    settings = {
        "arm": arm,
        "tier": "shared_controlled_sql_tool_harness_not_full_ui_agent",
        "model": "deepseek-flash",
        "thinking": "enabled",
        "reasoning_effort": "high",
        "rounds": 6,
        "sql_calls": 8,
        "case_timeout_seconds": 90,
        "concurrency": 2,
        "cases_sha256": hashlib.sha256(case_bytes).hexdigest(),
        "database_sha256": frozen["database_sha256"],
        "gate_sha256": hashlib.sha256(GATE.read_bytes()).hexdigest(),
        "governance_sha256": hashlib.sha256(GOVERNANCE.read_bytes()).hexdigest(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    settings_path = folder / "settings.json"
    if settings_path.exists():
        assert json.loads(settings_path.read_text()) == settings, (
            "Do not mix different experiments"
        )
    settings_path.write_text(json.dumps(settings, indent=2))
    semaphore = asyncio.Semaphore(2)
    async with httpx.AsyncClient(timeout=60) as client:

        async def bounded(case):
            path = folder / (case["id"] + ".json")
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
            async with semaphore:
                result = await one_case(case, arm, frozen, client)
                path.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
                print(
                    json.dumps(
                        {
                            "arm": arm,
                            "case": case["id"],
                            "correct": result["final_correct"],
                            "failure": result["failure"],
                        }
                    ),
                    flush=True,
                )
                return result

        results = await asyncio.gather(*(bounded(case) for case in frozen["cases"]))
    assert (
        hashlib.sha256(DATABASE.read_bytes()).hexdigest() == frozen["database_sha256"]
    )
    (folder / "results.json").write_text(
        json.dumps(
            {"settings": settings, "results": results},
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "arm": arm,
                "correct": sum(bool(item["final_correct"]) for item in results),
                "total": len(results),
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=["A", "B", "C"], required=True)
    asyncio.run(run(parser.parse_args().arm))
