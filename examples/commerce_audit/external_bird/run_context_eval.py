"""Paired BIRD comparison: bare DDL vs DDL plus schema context (no grain guard).

Arm "ddl" gets CREATE TABLE statements, BIRD's evidence text and the question.
Arm "context" also gets column notes, frequent values and matched values from
dbgpt_app.schema_context. One model sample per question and arm; correctness is
BIRD execution accuracy (equal result row sets).

Development databases (financial, student_club) were inspected while designing the
context; the held-out databases were not opened before the design was frozen.

    python examples/commerce_audit/external_bird/run_context_eval.py \
        --split heldout --label r1
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

from dbgpt_app import schema_context

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
BIRD = ROOT / "output/datatrail/bird"
DATABASES = BIRD / "extracted/dev_databases"
SPLITS = {
    "development": ["financial", "student_club"],
    "heldout": [
        "superhero",
        "california_schools",
        "toxicology",
        "debit_card_specializing",
    ],
}
MODEL = {
    "model": "deepseek-flash",
    "thinking": {"type": "enabled"},
    "reasoning_effort": "high",
}
SYSTEM = (
    "You are a SQLite expert. Write one SQLite query that answers the question using "
    "the schema. Return only the query in a ```sql code block."
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def execute(database: Path, sql: str, seconds: float = 60) -> dict:
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


def extract_sql(content: str) -> str:
    blocks = re.findall(r"```(?:sql|sqlite)?\s*(.*?)```", content or "", re.S | re.I)
    return (blocks[-1] if blocks else content or "").strip().rstrip(";")


class Database:
    """Everything about one database that does not depend on the question."""

    def __init__(self, db_id: str):
        self.path = DATABASES / db_id / f"{db_id}.sqlite"
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        connection.text_factory = lambda b: b.decode("utf-8", "replace")
        self.run = lambda sql: connection.execute(sql).fetchall()
        self.ddl = "\n\n".join(
            r[0]
            for r in self.run(
                "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL "
                "AND name NOT LIKE 'sqlite_%'"
            )
        )
        self.notes = schema_context.load_bird_notes(
            DATABASES / db_id / "database_description"
        )
        self.samples = schema_context.all_samples(self.run)
        self.index = schema_context.build_value_index(self.run)

    def prompt(self, case: dict, arm: str) -> str:
        parts = [f"Schema:\n{self.ddl}"]
        if arm == "context":
            parts.append(
                schema_context.build_context(
                    self.run, case["question"], self.notes, self.index, self.samples
                )
            )
        parts.append(f"External knowledge: {case.get('evidence') or ''}")
        parts.append(f"Question: {case['question']}")
        return "\n\n".join(parts)


async def ask(client: httpx.AsyncClient, prompt: str) -> tuple[str, dict]:
    base = os.environ.get("OPENAI_API_BASE", "https://api.deepseek.com/v1").rstrip("/")
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": prompt},
    ]
    for attempt in range(4):
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
            body = response.json()
            return body["choices"][0]["message"].get("content") or "", body.get(
                "usage", {}
            )
        except (httpx.HTTPError, KeyError):
            if attempt == 3:
                raise
            await asyncio.sleep(5 * (attempt + 1))
    raise RuntimeError("unreachable")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=SPLITS, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--arms", nargs="+", default=["ddl", "context"])
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    output = HERE / "context_runs" / f"{args.split}-{args.label}.json"
    if output.exists():
        raise SystemExit("Output exists; retain it and choose another label")
    load_dotenv(ROOT / ".env.local", override=False)
    dev = json.loads((BIRD / "dev.json").read_text(encoding="utf-8"))
    cases = [c for c in dev if c["db_id"] in SPLITS[args.split]]
    cases = cases[: args.limit] if args.limit else cases
    databases = {db: Database(db) for db in SPLITS[args.split]}
    golds = {
        c["question_id"]: execute(databases[c["db_id"]].path, c["SQL"]) for c in cases
    }
    semaphore = asyncio.Semaphore(args.concurrency)
    items: list[dict] = []

    async def one(case: dict, arm: str, client: httpx.AsyncClient) -> None:
        async with semaphore:
            database = databases[case["db_id"]]
            prompt = database.prompt(case, arm)
            item = {
                "question_id": case["question_id"],
                "db_id": case["db_id"],
                "arm": arm,
            }
            try:
                content, usage = await ask(client, prompt)
                sql = extract_sql(content)
                result = await asyncio.to_thread(execute, database.path, sql)
                gold = golds[case["question_id"]]
                item.update(
                    sql=sql,
                    error=result.get("error"),
                    correct="rows" in result
                    and "rows" in gold
                    and set(result["rows"]) == set(gold["rows"]),
                    prompt_characters=len(prompt),
                    usage=usage,
                )
            except Exception as exc:  # noqa: BLE001 -- record and keep the run going
                item.update(failure=type(exc).__name__, correct=False)
            items.append(item)
            if len(items) % 50 == 0:
                print(f"{len(items)} done", flush=True)

    async with httpx.AsyncClient(timeout=240) as client:
        await asyncio.gather(*(one(c, arm, client) for c in cases for arm in args.arms))
    items.sort(key=lambda i: (i["question_id"], i["arm"]))
    summary = {}
    for arm in args.arms:
        rows = [i for i in items if i["arm"] == arm]
        summary[arm] = {
            "questions": len(rows),
            "correct": sum(i["correct"] for i in rows),
            "accuracy": round(sum(i["correct"] for i in rows) / len(rows), 4),
            "sql_errors": sum(bool(i.get("error")) for i in rows),
            "failures": sum("failure" in i for i in rows),
            "by_db": {
                db: f"{sum(i['correct'] for i in rows if i['db_id'] == db)}/"
                f"{sum(i['db_id'] == db for i in rows)}"
                for db in SPLITS[args.split]
            },
            "prompt_tokens": sum(
                i.get("usage", {}).get("prompt_tokens", 0) for i in rows
            ),
            "completion_tokens": sum(
                i.get("usage", {}).get("completion_tokens", 0) for i in rows
            ),
        }
    payload = {
        "split": args.split,
        "label": args.label,
        "settings": {**MODEL, "samples_per_question_and_arm": 1},
        "dev_json_sha256": sha256(BIRD / "dev.json"),
        "schema_context_sha256": sha256(Path(schema_context.__file__)),
        "runner_sha256": sha256(Path(__file__)),
        "summary": summary,
        "items": items,
    }
    output.parent.mkdir(exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
