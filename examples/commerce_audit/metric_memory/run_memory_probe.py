"""Live check of cross-session metric memory on Olist with the real model.

Session 1: the user states a non-default GMV definition; the model must propose it
through the propose_metric_definition tool; a person (this script) confirms it.
Session 2 starts with no chat history. Each question is asked in four arms that differ
only in the memory section of the prompt:

- none:     memory disabled
- memory:   alice's confirmed definition loaded
- revoked:  same record after alice revoked it
- other:    bob asks; alice's memory must not apply

An answer follows the definition when its result equals the reference SQL written for
that definition. This is a functional check of the memory path, not an accuracy study.

    python examples/commerce_audit/metric_memory/run_memory_probe.py
"""

import asyncio
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import httpx
from dotenv import load_dotenv

from dbgpt_app.commerce_contract import load_contract
from dbgpt_app.commerce_memory import MetricDefinition, MetricMemory

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
DATABASE = ROOT / "examples/dashboard/olist/data/generated/olist.db"
MEMORY_DB = ROOT / "output/datatrail/metric-memory-probe.db"
DB = "olist_ecommerce_demo"
MODEL = {
    "model": "deepseek-flash",
    "thinking": {"type": "enabled"},
    "reasoning_effort": "high",
}
STATEMENT = (
    "以后我说的 GMV，都按商品金额加运费（price + freight_value）计算，"
    "按客户实际收货时间 order_delivered_customer_date 归属月份，"
    "只算 order_status='delivered' 的订单。"
)
DEFINED = (
    "SELECT ROUND(SUM(i.price + i.freight_value), 2) FROM olist_order_items i "
    "JOIN olist_orders o ON o.order_id = i.order_id WHERE o.order_status = 'delivered' "
    "AND strftime('%Y-%m', o.order_delivered_customer_date) = '{month}'"
)
MONTHS = ["2017-11", "2018-01", "2018-03", "2018-05"]
TOOL = {
    "type": "function",
    "function": {
        "name": "propose_metric_definition",
        "description": "记录用户明确说明的指标口径，待用户确认后才生效。",
        "parameters": {
            "type": "object",
            "properties": {
                "metric": {"type": "string"},
                "expression": {"type": "string"},
                "time_field": {"type": "string", "description": "只填列名"},
                "filters": {"type": "string", "description": "多个条件用英文分号分隔"},
                "currency": {
                    "type": "string",
                    "description": "三位币种代码，未说明则不填",
                },
                "note": {"type": "string"},
            },
            "required": ["metric", "expression"],
        },
    },
}
SYSTEM_SQL = (
    "你是 SQLite 数据分析助手。根据表结构和业务说明写一条 SQLite 查询回答问题，"
    "金额保留两位小数。只返回 ```sql 代码块。"
)


def run(sql: str):
    try:
        with closing(sqlite3.connect(DATABASE.as_uri() + "?mode=ro", uri=True)) as db:
            rows = db.execute(sql).fetchall()
        return [
            [round(v, 2) if isinstance(v, float) else v for v in row] for row in rows
        ]
    except Exception as exc:  # noqa: BLE001 -- failed SQL does not follow the definition
        return {"error": type(exc).__name__}


def ddl() -> str:
    with closing(sqlite3.connect(DATABASE.as_uri() + "?mode=ro", uri=True)) as db:
        return "\n\n".join(
            r[0]
            for r in db.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name LIKE 'olist_%'"
            )
        )


async def chat(client: httpx.AsyncClient, messages: list, tools=None) -> dict:
    base = os.environ.get("OPENAI_API_BASE", "https://api.deepseek.com/v1").rstrip("/")
    body = {**MODEL, "max_tokens": 8192, "messages": messages, "stream": False}
    if tools:
        body["tools"] = tools
    for attempt in range(4):
        try:
            response = await client.post(
                base + "/chat/completions",
                headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
                json=body,
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]
        except (httpx.HTTPError, KeyError):
            if attempt == 3:
                raise
            await asyncio.sleep(5 * (attempt + 1))
    raise RuntimeError("unreachable")


def extract_sql(content: str) -> str:
    blocks = re.findall(r"```(?:sql|sqlite)?\s*(.*?)```", content or "", re.S | re.I)
    return (blocks[-1] if blocks else content or "").strip().rstrip(";")


async def main() -> None:
    load_dotenv(ROOT / ".env.local", override=False)
    MEMORY_DB.unlink(missing_ok=True)
    memory = MetricMemory(MEMORY_DB)
    contract = load_contract("olist")
    schema = ddl()
    report: dict = {"statement": STATEMENT, "model": MODEL["model"]}
    async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
        # Session 1: the model proposes (a rejected field goes back to it once);
        # a person confirms.
        messages = [
            {
                "role": "system",
                "content": "你是数据分析助手。用户说明指标口径时，调用 "
                "propose_metric_definition 记录，不要自行补充用户没说的条件。\n"
                + schema,
            },
            {"role": "user", "content": STATEMENT},
        ]
        attempts = []
        definition = None
        for _ in range(2):
            message = await chat(client, messages, [TOOL])
            calls = message.get("tool_calls") or []
            if not calls:
                attempts.append({"tool_call": None})
                break
            args = json.loads(calls[0]["function"]["arguments"])
            try:
                definition = MetricDefinition(
                    metric=args["metric"],
                    expression=args["expression"],
                    time_field=args.get("time_field") or None,
                    filters=[
                        f.strip()
                        for f in (args.get("filters") or "").split(";")
                        if f.strip()
                    ],
                    currency=args.get("currency") or None,
                    note=args.get("note") or "",
                )
                attempts.append({"arguments": args, "accepted": True})
                break
            except ValueError as exc:
                error = (
                    f"口径未记录：{exc.errors()[0]['loc'][0]} 格式不符，"
                    "请只填列名或三位币种代码后重试"
                )
                attempts.append({"arguments": args, "accepted": False, "error": error})
                messages += [
                    {
                        k: message[k]
                        for k in ("role", "content", "tool_calls")
                        if k in message
                    }
                    | {"reasoning_content": message.get("reasoning_content", "")},
                    {"role": "tool", "tool_call_id": calls[0]["id"], "content": error},
                ]
        report["session1_attempts"] = attempts
        if definition is None:
            out = HERE / "RESULTS.json"
            out.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8"
            )
            raise SystemExit("model did not produce a valid proposal")
        record = memory.propose("alice", DB, definition, "probe-session-1", STATEMENT)
        memory.confirm(record["id"], "alice", contract)
        prompts = {
            "none": "",
            "memory": memory.prompt("alice", DB, contract),
            "other": memory.prompt("bob", DB, contract),
        }
        memory.revoke(record["id"], "alice")
        prompts["revoked"] = memory.prompt("alice", DB, contract)
        report["memory_sections"] = prompts

        # Session 2: no chat history; only the memory section differs by arm.
        cases = []
        for month in MONTHS:
            question = f"{month} 的 GMV 是多少？"
            expected = run(DEFINED.format(month=month))
            for arm, section in prompts.items():
                content = "\n\n".join(
                    part
                    for part in [
                        schema,
                        contract.prompt_summary(),
                        section,
                        "问题：" + question,
                    ]
                    if part
                )
                answer = await chat(
                    client,
                    [
                        {"role": "system", "content": SYSTEM_SQL},
                        {"role": "user", "content": content},
                    ],
                )
                sql = extract_sql(answer.get("content") or "")
                result = run(sql)
                cases.append(
                    {
                        "month": month,
                        "arm": arm,
                        "follows_definition": result == expected,
                        "uses_freight": "freight_value" in sql,
                        "uses_delivery_time": "order_delivered_customer_date" in sql,
                        "result": result,
                        "expected": expected,
                        "sql": sql,
                    }
                )
                print(json.dumps(cases[-1], ensure_ascii=False)[:300], flush=True)
    report["cases"] = cases
    report["summary"] = {
        arm: f"{sum(c['follows_definition'] for c in cases if c['arm'] == arm)}"
        f"/{len(MONTHS)}"
        for arm in prompts
    }
    out = HERE / "RESULTS.json"
    out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(report["summary"])


if __name__ == "__main__":
    asyncio.run(main())
