"""Run frozen synthetic challenges through the existing DeepSeek SQL harness."""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import httpx
import sqlglot
from dotenv import load_dotenv
from run_offline import ROOT, create_database, python_oracle, rows

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "commerce_evaluation_v1", HERE.parent / "evaluation_v1/run_comparison.py"
)
EVALUATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVALUATOR)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(report, path):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


async def run(label):
    if not re.fullmatch(r"[a-z0-9-]{1,40}", label):
        raise ValueError("Use 1-40 lowercase letters, digits or hyphens for run label")
    output = HERE / ("MODEL_RESULTS-" + label + ".json")
    if output.exists():
        raise ValueError("Output exists; retain it and choose another run label")
    manifest_path = HERE / "cases.json"
    frozen = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert digest(HERE / "run_offline.py") == frozen["fixture_source_sha256"]
    load_dotenv(ROOT / ".env.local", override=False)
    if not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("Configure local OPENAI_API_KEY before running")
    report = {
        "tier": frozen["tier"],
        "run_label": label,
        "state": "running",
        "settings": {
            "model": "deepseek-flash",
            "thinking": "enabled",
            "reasoning_effort": "high",
            "rounds": 6,
            "sql_calls": 8,
            "case_timeout_seconds": 90,
            "concurrency": 1,
            "order": "B then C per case; one sample, no retry",
            "cases_sha256": digest(manifest_path),
            "fixture_source_sha256": frozen["fixture_source_sha256"],
            "runner_sha256": digest(Path(__file__)),
            "evaluator_sha256": digest(HERE.parent / "evaluation_v1/run_comparison.py"),
            "gate_sha256": digest(EVALUATOR.GATE),
            "governance_sha256": digest(EVALUATOR.GOVERNANCE),
        },
        "notes": frozen["notes"]
        + [
            "B/C both get full upstream governance; only grain gate differs.",
            "Synthetic first-SQL instructions are explicit in I01/I02.",
            "Same author/fixture, small sample; "
            "no natural accuracy/generalization claim.",
            "Fixed order and provider caching confound timing; no speedup claim.",
            "Usage is provider-reported, not billed currency cost.",
        ],
        "results": [],
    }
    with tempfile.TemporaryDirectory(prefix="datatrail-model-challenge-") as temporary:
        database = Path(temporary) / "challenge.db"
        create_database(database, frozen["fixture_scenario"])
        with closing(sqlite3.connect(database)) as connection:
            oracle = python_oracle(connection)
            assert oracle["both"] == [[50, 58]]
            for case in frozen["cases"]:
                assert rows(connection, case["reference_sql"]) == case["expected_rows"]
        EVALUATOR.DATABASE = database
        save(report, output)
        async with httpx.AsyncClient(timeout=90) as client:
            for case in frozen["cases"]:
                for arm in ("B", "C"):
                    os.environ["DATATRAIL_GRAIN_GUARD"] = "1" if arm == "C" else "0"
                    result = await EVALUATOR.one_case(case, arm, frozen, client)
                    if case.get("required_first_sql"):
                        first = result["tools"][0]["sql"] if result["tools"] else ""
                        try:
                            result["protocol_adherence"] = bool(first) and (
                                sqlglot.parse_one(first, read="sqlite")
                                == sqlglot.parse_one(
                                    case["required_first_sql"], read="sqlite"
                                )
                            )
                        except sqlglot.errors.ParseError:
                            result["protocol_adherence"] = False
                    else:
                        result["protocol_adherence"] = None
                    result["gate_rejections"] = sum(
                        tool.get("error_code") == "COMMERCE_GRAIN_REVIEW_REQUIRED"
                        for tool in result["tools"]
                    )
                    report["results"].append(result)
                    save(report, output)
                    print(
                        json.dumps(
                            {
                                "case_id": case["id"],
                                "arm": arm,
                                "final_correct": result["final_correct"],
                                "last_sql_correct": result["last_sql_correct"],
                                "gate_rejections": result["gate_rejections"],
                                "protocol_adherence": result["protocol_adherence"],
                                "failure": result["failure"],
                            }
                        ),
                        flush=True,
                    )
        report["state"] = "completed"
        save(report, output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-label", required=True)
    asyncio.run(run(parser.parse_args().run_label))
