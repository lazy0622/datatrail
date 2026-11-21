"""Let DeepSeek write SQL for BIRD dev questions; measure what the grain guard adds.

Paired design: each question gets one model SQL. The unguarded arm executes it as is.
The guarded arm checks the same SQL first; only flagged SQL is sent back to the model
with the guard's message, for at most two rewrites. Correctness is BIRD execution
accuracy: the result row set equals the gold SQL's row set.

    python examples/commerce_audit/external_bird/run_model_bird.py --output <new file>
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import httpx
from dotenv import load_dotenv

from dbgpt_app.commerce_contract import load_contract
from dbgpt_app.commerce_grain import grain_review

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
GUARD = ROOT / "packages/dbgpt-app/src/dbgpt_app/commerce_grain.py"
CONTRACTS = {
    "financial": "bird_financial.json",
    "student_club": "bird_student_club.json",
}
MODEL = {
    "model": "deepseek-flash",
    "thinking": {"type": "enabled"},
    "reasoning_effort": "high",
}
MAX_REWRITES = 2
SYSTEM = (
    "You are a SQLite expert. Write one SQLite query that answers the question using "
    "the schema. Return only the query in a ```sql code block."
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def schema(database: Path) -> str:
    with closing(sqlite3.connect(database)) as connection:
        rows = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"
        ).fetchall()
    return "\n\n".join(row[0] for row in rows)


def execute(database: Path, sql: str, seconds: float = 60) -> dict:
    """Run read-only with a deadline; return a row set or the error type."""
    try:
        with closing(
            sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        ) as connection:
            deadline = time.monotonic() + seconds
            connection.set_progress_handler(
                lambda: int(time.monotonic() > deadline), 10000
            )
            return {"rows": connection.execute(sql).fetchall()}
    except Exception as exc:  # noqa: BLE001 -- any SQL failure is an incorrect answer
        return {"error": type(exc).__name__}


def same(left: dict, right: dict) -> bool:
    return (
        "rows" in left and "rows" in right and set(left["rows"]) == set(right["rows"])
    )


def extract_sql(content: str) -> str:
    blocks = re.findall(r"```(?:sql|sqlite)?\s*(.*?)```", content or "", re.S | re.I)
    return (blocks[-1] if blocks else content or "").strip().rstrip(";")


def review(sql: str, contract) -> dict | None:
    try:
        return grain_review(sql, contract=contract)
    except Exception as exc:  # noqa: BLE001 -- unparseable SQL is not a guard hit
        return {"parse_error": type(exc).__name__}


async def ask(client: httpx.AsyncClient, messages: list, usage: list) -> str:
    base = os.environ.get("OPENAI_API_BASE", "https://api.deepseek.com/v1").rstrip("/")
    for attempt in range(3):
        try:
            response = await client.post(
                base + "/chat/completions",
                headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
                json={
                    **MODEL,
                    "max_tokens": 8192,
                    "messages": messages,
                    "stream": False,
                },
            )
            response.raise_for_status()
            break
        except httpx.HTTPError:
            if attempt == 2:
                raise
            await asyncio.sleep(5 * (attempt + 1))
    body = response.json()
    usage.append(body.get("usage", {}))
    return body["choices"][0]["message"].get("content") or ""


async def one(case: dict, database: Path, contract, prompt_schema: str, client) -> dict:
    usage: list = []
    messages = [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": f"Schema:\n{prompt_schema}\n\n"
            f"External knowledge: {case.get('evidence') or ''}"
            f"\n\nQuestion: {case['question']}",
        },
    ]
    started = time.monotonic()
    gold = await asyncio.to_thread(execute, database, case["SQL"])
    content = await ask(client, messages, usage)
    first_sql = extract_sql(content)
    first = await asyncio.to_thread(execute, database, first_sql)
    hit = review(first_sql, contract)
    item = {
        "question_id": case["question_id"],
        "db_id": case["db_id"],
        "difficulty": case.get("difficulty"),
        "gold_error": gold.get("error"),
        "first_sql": first_sql,
        "first_error": first.get("error"),
        "unguarded_correct": same(first, gold),
        "guard_hit": None,
        "rewrites": [],
    }
    final_sql = first_sql
    if hit and "parse_error" in hit:
        item["guard_parse_error"] = hit["parse_error"]
        hit = None
    if hit:
        item["guard_hit"] = {k: hit[k] for k in ("rule_id", "expression")}
        for _ in range(MAX_REWRITES):
            messages += [
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": "The query was stopped before execution by the metric "
                    f"grain check (rule {hit['rule_id']}, "
                    f"expression {hit['expression']}): "
                    f"{hit['reason']} Rewrite it so each amount is counted once. "
                    "Return only the query in a ```sql code block.",
                },
            ]
            content = await ask(client, messages, usage)
            final_sql = extract_sql(content)
            hit = review(final_sql, contract)
            if hit and "parse_error" in hit:
                hit = None
            item["rewrites"].append({"sql": final_sql, "still_flagged": bool(hit)})
            if not hit:
                break
    if hit:
        # Still flagged after the rewrite budget: the system would hand it to a person.
        item["guarded_outcome"] = "sent_to_review"
        item["guarded_correct"] = False
    else:
        final = (
            first
            if final_sql == first_sql
            else await asyncio.to_thread(execute, database, final_sql)
        )
        item["guarded_outcome"] = "answered"
        item["guarded_correct"] = same(final, gold)
        if item["guard_hit"]:
            item["rewrite_changed_result"] = not same(final, first)
    item["model_calls"] = len(usage)
    item["usage"] = usage
    item["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return item


def summarize(items: list[dict]) -> dict:
    hits = [i for i in items if i["guard_hit"]]
    return {
        "questions": len(items),
        "unguarded_correct": sum(i["unguarded_correct"] for i in items),
        "guarded_correct": sum(i["guarded_correct"] for i in items),
        "guard_hits": len(hits),
        "hits_on_wrong_first_sql": sum(not i["unguarded_correct"] for i in hits),
        "hits_on_correct_first_sql": sum(i["unguarded_correct"] for i in hits),
        "hits_fixed_by_rewrite": sum(
            i["guarded_correct"] and not i["unguarded_correct"] for i in hits
        ),
        "hits_broken_by_rewrite": sum(
            i["unguarded_correct"] and not i["guarded_correct"] for i in hits
        ),
        "sent_to_review": sum(i["guarded_outcome"] == "sent_to_review" for i in items),
        "avg_hits": sum(
            i["guard_hit"]["expression"].upper().startswith("AVG") for i in hits
        ),
        "model_calls": sum(i["model_calls"] for i in items),
        "prompt_tokens": sum(
            u.get("prompt_tokens", 0) for i in items for u in i["usage"]
        ),
        "completion_tokens": sum(
            u.get("completion_tokens", 0) for i in items for u in i["usage"]
        ),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bird-dir", type=Path, default=ROOT / "output/datatrail/bird")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Output exists; retain it and choose a new output path")
    load_dotenv(ROOT / ".env.local", override=False)
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("Configure local OPENAI_API_KEY before running")
    dev_path = args.bird_dir / "dev.json"
    cases = [
        c
        for c in json.loads(dev_path.read_text(encoding="utf-8"))
        if c["db_id"] in CONTRACTS
    ]
    cases = cases[: args.limit] if args.limit else cases
    contracts = {
        db: load_contract(ROOT / "configs/metric_contracts" / name)
        for db, name in CONTRACTS.items()
    }
    schemas = {db: schema(args.bird_dir / f"{db}.sqlite") for db in CONTRACTS}
    semaphore = asyncio.Semaphore(args.concurrency)
    items: list[dict] = []

    async def run(case: dict, client: httpx.AsyncClient) -> None:
        async with semaphore:
            db = case["db_id"]
            try:
                item = await one(
                    case,
                    args.bird_dir / f"{db}.sqlite",
                    contracts[db],
                    schemas[db],
                    client,
                )
            except Exception as exc:  # noqa: BLE001 -- keep the run; record the failure
                item = {
                    "question_id": case["question_id"],
                    "db_id": db,
                    "failure": type(exc).__name__,
                }
            items.append(item)
            print(
                json.dumps(
                    {
                        k: item.get(k)
                        for k in (
                            "question_id",
                            "unguarded_correct",
                            "guard_hit",
                            "guarded_correct",
                            "failure",
                        )
                    }
                ),
                flush=True,
            )

    async with httpx.AsyncClient(timeout=180) as client:
        await asyncio.gather(*(run(case, client) for case in cases))
    items.sort(key=lambda i: i["question_id"])
    completed = [i for i in items if "failure" not in i]
    payload = {
        "tier": "external_benchmark_model_generated_sql",
        "benchmark": "BIRD dev (dev_20240627), financial + student_club",
        "settings": {**MODEL, "max_rewrites": MAX_REWRITES, "samples_per_question": 1},
        "dev_json_sha256": sha256(dev_path),
        "gate_sha256": sha256(GUARD),
        "runner_sha256": sha256(Path(__file__)),
        "contract_sha256": {
            db: sha256(ROOT / "configs/metric_contracts" / n)
            for db, n in CONTRACTS.items()
        },
        "failures": [i for i in items if "failure" in i],
        "summary": {
            "all": summarize(completed),
            **{
                db: summarize([i for i in completed if i["db_id"] == db])
                for db in CONTRACTS
            },
        },
        "items": items,
        "notes": [
            "One model sample per question; no retry of the first SQL.",
            "Contracts and guard were frozen before this run.",
            "Execution accuracy compares row sets, as BIRD does; "
            "it ignores row order and duplicates.",
        ],
    }
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
