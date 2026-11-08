"""Publish whitelisted paired outcomes; leave raw model traces local."""

import ast
import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from examples.commerce_audit.evaluation_v1.run_comparison import rows_match  # noqa: E402


def main():
    frozen = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
    expected = {case["id"]: case for case in frozen["cases"]}
    arms = {}
    details = {}
    settings = {}
    for arm in ("A", "B", "C"):
        raw = json.loads(
            (ROOT / "output/datatrail/evaluation-v1" / arm / "results.json").read_text(
                encoding="utf-8"
            )
        )
        settings[arm] = raw["settings"]
        rows = raw["results"]
        for row in rows:
            row["recorded_last_sql_correct_before_row_normalization"] = row[
                "last_sql_correct"
            ]
            executed = row["executed_queries"]
            # The first run used default=str for SQLAlchemy Row. Decode only
            # those persisted Python tuples; do not rerun the model or queries.
            normalized = [
                ast.literal_eval(value) if isinstance(value, str) else value
                for value in (executed[-1]["rows"] if executed else [])
            ]
            row["last_sql_correct"] = (
                None
                if expected[row["case_id"]].get("unsupported")
                else bool(
                    executed
                    and rows_match(
                        normalized, expected[row["case_id"]]["expected_rows"]
                    )
                )
            )
        assert len(rows) == 20 and len({row["case_id"] for row in rows}) == 20
        details[arm] = {
            row["case_id"]: {
                **{
                    key: row[key]
                    for key in (
                        "case_id",
                        "split",
                        "completed",
                        "final_correct",
                        "last_sql_correct",
                        "failure",
                        "elapsed_seconds",
                        "model_calls",
                        "recorded_last_sql_correct_before_row_normalization",
                    )
                },
                "sql_calls": len(row["tools"]),
                "executed_queries": len(row["executed_queries"]),
                "gate_rejections": sum(
                    bool((tool.get("error_code") or "").startswith("COMMERCE_"))
                    for tool in row["tools"]
                ),
                "rejected_queries_matching_reference": sum(
                    tool.get("rejected_rows_match_reference") is True
                    for tool in row["tools"]
                ),
                "final": row["final"],
                "sql_trace": row["tools"],
                "usage": row["usage"],
            }
            for row in rows
        }
        durations = [row["elapsed_seconds"] for row in rows]
        usage = {
            key: sum(item.get(key, 0) or 0 for row in rows for item in row["usage"])
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "prompt_cache_hit_tokens",
                "prompt_cache_miss_tokens",
            )
        }
        arms[arm] = {
            "total": 20,
            "completed": sum(row["completed"] for row in rows),
            "final_correct": sum(row["final_correct"] for row in rows),
            "development_correct": sum(
                row["final_correct"] and row["split"] == "development" for row in rows
            ),
            "reserved_correct": sum(
                row["final_correct"] and row["split"] == "reserved" for row in rows
            ),
            "last_sql_correct_of_19_numeric_cases": sum(
                row["last_sql_correct"] is True for row in rows
            ),
            "mean_seconds": round(statistics.mean(durations), 2),
            "median_seconds": round(statistics.median(durations), 2),
            "maximum_seconds": max(durations),
            "model_calls": sum(row["model_calls"] for row in rows),
            "sql_calls": sum(len(row["tools"]) for row in rows),
            "gate_rejections": sum(
                row["gate_rejections"] for row in details[arm].values()
            ),
            "rejected_queries_matching_reference": sum(
                row["rejected_queries_matching_reference"]
                for row in details[arm].values()
            ),
            "usage": usage,
        }
    for name in ("cases_sha256", "database_sha256", "runner_sha256", "gate_sha256"):
        assert len({settings[arm][name] for arm in settings}) == 1
    paired = {}
    for before, after in (("A", "B"), ("B", "C"), ("A", "C")):
        paired[before + "_to_" + after] = {
            "wrong_to_correct": [
                case
                for case in details[before]
                if not details[before][case]["final_correct"]
                and details[after][case]["final_correct"]
            ],
            "correct_to_wrong": [
                case
                for case in details[before]
                if details[before][case]["final_correct"]
                and not details[after][case]["final_correct"]
            ],
        }
    report = {
        "tier": "controlled_sql_tool_comparison_not_full_ui_agent",
        "metric_name": "predefined_business_contract_conformance",
        "arm_definitions": {
            "A": "basic schema+SQL tool, no DataTrail gate or reuse",
            "B": "A+full upstream governance document",
            "C": "B+expanded grain gate; query reuse still disabled",
        },
        "split": {"development": 8, "reserved": 12},
        "notes": [
            (
                "H05/H07/H12 omit an explicit purchase-vs-delivery time basis. "
                "A selected delivery time; the oracle and governance document "
                "select purchase time. Those score differences measure the "
                "predefined default contract, not universally wrong facts. "
                "Original frozen questions are retained without post-hoc edits."
            ),
            (
                "Collector-only correction: SQLAlchemy rows serialized as tuple "
                "strings in the initial raw logs. SQL-result grading was "
                "recomputed from those stored tuples without model/query reruns; "
                "original logs and settings hashes retained. Final-answer "
                "grading was unaffected. The current runner normalizes rows."
            ),
            (
                "Fixed order A then B then C, one sample per case/arm; "
                "cache and time-of-run effects are not controlled."
            ),
            (
                "The evaluator author curated both splits; reserved questions "
                "were frozen before code changes, not externally blinded."
            ),
            (
                "Identical results of a rejected query on this database are "
                "evidence for review, not proof of SQL semantic equivalence."
            ),
            (
                "No post-outcome prompt tuning or replacement of failed cases. "
                "No full UI multi-tool-agent or production speed claim."
            ),
            (
                "Monetary cost not returned; provider-reported token usage "
                "retained without claiming actual billed currency cost."
            ),
        ],
        "settings": settings,
        "arms": arms,
        "paired": paired,
        "cases": details,
    }
    (HERE / "RESULTS.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"arms": arms, "paired": paired}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
