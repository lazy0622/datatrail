"""Inspect the bundled synthetic data; this is not an LLM evaluation."""

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def inspect_database(path: Path) -> dict:
    path = path.resolve(strict=True)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only = ON")
        metadata = dict(connection.execute("SELECT key, value FROM dataset_metadata"))
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        orders = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        fanout_orders = connection.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT o.order_id FROM orders o "
            "JOIN order_items i ON i.order_id=o.order_id "
            "JOIN order_payments p ON p.order_id=o.order_id "
            "GROUP BY o.order_id "
            "HAVING COUNT(DISTINCT i.order_item_id)>1 "
            "AND COUNT(DISTINCT p.payment_sequential)>1)"
        ).fetchone()[0]
        item_total = connection.execute(
            "SELECT ROUND(SUM(i.price), 2) FROM orders o "
            "JOIN order_items i ON i.order_id=o.order_id "
            "WHERE o.order_status='delivered'"
        ).fetchone()[0]
        joined_total = connection.execute(
            "SELECT ROUND(SUM(i.price), 2) FROM orders o "
            "JOIN order_items i ON i.order_id=o.order_id "
            "JOIN order_payments p ON p.order_id=o.order_id "
            "WHERE o.order_status='delivered'"
        ).fetchone()[0]

    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return {
        "evaluation_tier": "read_only_data_diagnostic_not_llm_evaluation",
        "database": str(path),
        "database_sha256": digest,
        "dataset_metadata": metadata,
        "tables": tables,
        "order_count": orders,
        "orders_with_multiple_items_and_payments": fanout_orders,
        "hand_written_sql_counterexample": {
            "metric": "delivered item price sum, excluding freight",
            "without_payment_join": item_total,
            "with_naive_payment_join": joined_total,
        },
        "limitations": [
            "No model was called and no model failure was measured.",
            "The bundled database declares synthetic_demo, "
            "not real Olist transactions.",
            "No modification gain or general SQL-checking capability is established.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, default=ROOT / "pilot/examples/Olist_Ecommerce.db"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = inspect_database(args.db)
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        if args.output.resolve() == args.db.resolve():
            parser.error("--output must not overwrite the input database")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")


if __name__ == "__main__":
    main()
