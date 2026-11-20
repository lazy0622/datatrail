"""Scan BIRD dev gold SQL with the metric-contract guard (no model calls).

Each contract is written from schema cardinalities that this script re-checks on the
data, not from the gold SQL. Reviewed queries get a manual verdict in adjudication.json.

    python examples/commerce_audit/external_bird/scan_bird.py --bird-dir <BIRD dev dir>
"""

import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from dbgpt_app.commerce_contract import load_contract
from dbgpt_app.commerce_grain import grain_review

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def many_per_key(table: str, key: str) -> str:
    return (
        f'SELECT count(*) FROM (SELECT {key} FROM "{table}" '
        f"GROUP BY {key} HAVING count(*) > 1)"
    )


DATABASES = {
    "financial": {
        "contract": "bird_financial.json",
        "cardinality": {
            "accounts_with_multiple_disp": many_per_key("disp", "account_id"),
            "accounts_with_multiple_owner_disp": (
                "SELECT count(*) FROM (SELECT account_id FROM disp WHERE type='OWNER' "
                "GROUP BY account_id HAVING count(*) > 1)"
            ),
            "accounts_with_multiple_loan": many_per_key("loan", "account_id"),
            "accounts_with_multiple_trans": many_per_key("trans", "account_id"),
            "accounts_with_multiple_order": many_per_key("order", "account_id"),
        },
    },
    "student_club": {
        "contract": "bird_student_club.json",
        "cardinality": {
            "budgets_with_multiple_expense": many_per_key("expense", "link_to_budget"),
            "events_with_multiple_attendance": many_per_key(
                "attendance", "link_to_event"
            ),
            "members_with_multiple_income": many_per_key("income", "link_to_member"),
            "members_with_multiple_expense": many_per_key("expense", "link_to_member"),
        },
    },
}


def run(connection: sqlite3.Connection, sql: str) -> list:
    return [list(row) for row in connection.execute(sql).fetchall()[:20]]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bird-dir", type=Path, default=ROOT / "output/datatrail/bird")
    parser.add_argument("--output", type=Path, default=HERE / "RESULTS.json")
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Output exists; retain it and choose a new output path")
    dev_path = args.bird_dir / "dev.json"
    dev = json.loads(dev_path.read_text(encoding="utf-8"))
    adjudication = {
        item["question_id"]: item
        for item in json.loads((HERE / "adjudication.json").read_text(encoding="utf-8"))
    }
    databases = {}
    for db_id, spec in DATABASES.items():
        contract_path = ROOT / "configs/metric_contracts" / spec["contract"]
        contract = load_contract(contract_path)
        cases = [case for case in dev if case["db_id"] == db_id]
        reviewed = []
        with closing(sqlite3.connect(args.bird_dir / f"{db_id}.sqlite")) as connection:
            cardinality = {
                name: connection.execute(sql).fetchone()[0]
                for name, sql in spec["cardinality"].items()
            }
            for case in cases:
                hit = grain_review(case["SQL"], contract=contract)
                if hit is None:
                    continue
                item = {
                    "question_id": case["question_id"],
                    "question": case["question"],
                    "evidence": case.get("evidence"),
                    "gold_sql": case["SQL"],
                    "rule_id": hit["rule_id"],
                    "gold_result": run(connection, case["SQL"]),
                }
                judged = adjudication.get(case["question_id"], {})
                item["verdict"] = judged.get("verdict", "unjudged")
                item["note"] = judged.get("note")
                if judged.get("rewrite_sql"):
                    item["rewrite_sql"] = judged["rewrite_sql"]
                    item["rewrite_result"] = run(connection, judged["rewrite_sql"])
                    item["results_differ"] = (
                        item["rewrite_result"] != item["gold_result"]
                    )
                reviewed.append(item)
        verdicts: dict[str, int] = {}
        for item in reviewed:
            verdicts[item["verdict"]] = verdicts.get(item["verdict"], 0) + 1
        databases[db_id] = {
            "contract_sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
            "cardinality_checks": cardinality,
            "questions": len(cases),
            "reviewed": len(reviewed),
            "verdicts": verdicts,
            "items": reviewed,
        }
    payload = {
        "tier": "external_benchmark_gold_sql_offline_scan",
        "model_calls": 0,
        "benchmark": "BIRD dev (dev_20240627)",
        "dev_json_sha256": hashlib.sha256(dev_path.read_bytes()).hexdigest(),
        "gate_sha256": hashlib.sha256(
            (ROOT / "packages/dbgpt-app/src/dbgpt_app/commerce_grain.py").read_bytes()
        ).hexdigest(),
        "databases": databases,
        "notes": [
            "Verdicts are the author's manual reading, not BIRD labels.",
            "Only SUM is checked; AVG over repeated rows is outside the guard.",
            "Questions that were not reviewed are not claimed to be correct.",
        ],
    }
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                db: {k: v[k] for k in ("questions", "reviewed", "verdicts")}
                for db, v in databases.items()
            }
        )
    )


if __name__ == "__main__":
    main()
