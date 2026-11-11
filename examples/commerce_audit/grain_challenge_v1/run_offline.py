"""Synthetic SQL mutation challenge; never a natural-task accuracy benchmark."""

import argparse
import hashlib
import json
import sqlite3
import tempfile
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from dbgpt_app.commerce_grain import grain_review

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SCHEMA = """
CREATE TABLE olist_orders (
    order_id TEXT PRIMARY KEY, order_status TEXT,
    order_purchase_timestamp TEXT
);
CREATE TABLE olist_order_items (
    order_id TEXT, order_item_id INTEGER, price REAL, freight_value REAL
);
CREATE TABLE olist_order_payments (
    order_id TEXT, payment_sequential INTEGER, payment_value REAL
);
"""
SCOPE = (
    "o.order_status='delivered' AND "
    "o.order_purchase_timestamp>='2018-07-01' AND "
    "o.order_purchase_timestamp<'2018-08-01'"
)
RAW = (
    " FROM olist_orders o "
    "JOIN olist_order_items i ON i.order_id=o.order_id "
    "JOIN olist_order_payments p ON p.order_id=o.order_id "
    "WHERE " + SCOPE
)
PREAGGREGATED = (
    "WITH i AS (SELECT order_id,SUM(price) product,SUM(freight_value) freight "
    "FROM olist_order_items GROUP BY order_id), "
    "p AS (SELECT order_id,SUM(payment_value) payment "
    "FROM olist_order_payments GROUP BY order_id) "
)
JOIN_TOTALS = (
    " FROM olist_orders o JOIN i ON i.order_id=o.order_id "
    "JOIN p ON p.order_id=o.order_id WHERE " + SCOPE
)
REFERENCES = {
    "product": "SELECT SUM(i.price) FROM olist_orders o "
    "JOIN olist_order_items i ON i.order_id=o.order_id WHERE "
    + SCOPE
    + " AND EXISTS(SELECT 1 FROM olist_order_payments p "
    "WHERE p.order_id=o.order_id)",
    "payment": "SELECT SUM(p.payment_value) FROM olist_orders o "
    "JOIN olist_order_payments p ON p.order_id=o.order_id WHERE "
    + SCOPE
    + " AND EXISTS(SELECT 1 FROM olist_order_items i WHERE i.order_id=o.order_id)",
    "product_freight": PREAGGREGATED + "SELECT SUM(i.product+i.freight)" + JOIN_TOTALS,
    "both": PREAGGREGATED + "SELECT SUM(i.product),SUM(p.payment)" + JOIN_TOTALS,
    "orders": "SELECT COUNT(DISTINCT o.order_id)" + RAW,
}
CASES = [
    {
        "id": "R01",
        "category": "risky_raw_join",
        "metric": "product",
        "sql": "SELECT SUM(i.price)" + RAW,
    },
    {
        "id": "R02",
        "category": "risky_raw_join",
        "metric": "payment",
        "sql": "SELECT SUM(p.payment_value)" + RAW,
    },
    {
        "id": "R03",
        "category": "risky_raw_join",
        "metric": "product_freight",
        "sql": "SELECT SUM(i.price+i.freight_value)" + RAW,
    },
    {
        "id": "R04",
        "category": "risky_distinct_amount",
        "metric": "product",
        "sql": "SELECT SUM(DISTINCT i.price)" + RAW,
    },
    {
        "id": "L01",
        "category": "valid_control",
        "metric": "product",
        "sql": REFERENCES["product"],
    },
    {
        "id": "L02",
        "category": "valid_control",
        "metric": "payment",
        "sql": REFERENCES["payment"],
    },
    {
        "id": "L03",
        "category": "valid_control",
        "metric": "both",
        "sql": REFERENCES["both"],
    },
    {
        "id": "L04",
        "category": "valid_control",
        "metric": "orders",
        "sql": REFERENCES["orders"],
    },
    {
        "id": "L05",
        "category": "valid_control",
        "metric": "product_freight",
        "sql": REFERENCES["product_freight"],
    },
    {
        "id": "L06",
        "category": "valid_control",
        "metric": "product",
        "contract": "Only require a payment record; sequence 1 exists per order.",
        "sql": "SELECT SUM(i.price)" + RAW + " AND p.payment_sequential=1",
    },
    {
        "id": "L07",
        "category": "explicit_allocation_control",
        "metric": "product",
        "contract": (
            "Product amount allocated proportionally over payments, then totalled."
        ),
        "sql": "WITH t AS (SELECT order_id,SUM(payment_value) total "
        "FROM olist_order_payments GROUP BY order_id) "
        "SELECT SUM(i.price*p.payment_value/t.total) "
        "FROM olist_orders o JOIN olist_order_items i ON i.order_id=o.order_id "
        "JOIN olist_order_payments p ON p.order_id=o.order_id "
        "JOIN t ON t.order_id=o.order_id WHERE " + SCOPE,
    },
    {
        "id": "U01",
        "category": "unsupported_unqualified_column",
        "metric": "product",
        "sql": "SELECT SUM(price)" + RAW,
    },
    {
        "id": "U02",
        "category": "unsupported_partial_preaggregation",
        "metric": "product",
        "sql": "WITH i AS (SELECT order_id,SUM(price) amount "
        "FROM olist_order_items GROUP BY order_id) SELECT SUM(i.amount) "
        "FROM olist_orders o JOIN i ON i.order_id=o.order_id "
        "JOIN olist_order_payments p ON p.order_id=o.order_id WHERE " + SCOPE,
    },
]


def create_database(path, scenario):
    items = [("o1", 1, 10, 1), ("o1", 2, 10, 2), ("o2", 1, 30, 3)]
    payments = [("o1", 1, 25), ("o2", 1, 33)]
    if scenario in {"split_items", "split_both"}:
        items = items[:-1] + [("o2", 1, 12, 1), ("o2", 2, 18, 2)]
    if scenario in {"split_payments", "split_both"}:
        payments = [("o1", 1, 10), ("o1", 2, 15), ("o2", 1, 33)]
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(SCHEMA)
        connection.executemany(
            "INSERT INTO olist_orders VALUES (?,?,?)",
            [
                ("o1", "delivered", "2018-07-02"),
                ("o2", "delivered", "2018-07-03"),
                ("o3", "canceled", "2018-07-03"),
            ],
        )
        connection.executemany(
            "INSERT INTO olist_order_items VALUES (?,?,?,?)", items + [("o3", 1, 99, 9)]
        )
        connection.executemany(
            "INSERT INTO olist_order_payments VALUES (?,?,?)",
            payments + [("o3", 1, 108)],
        )


def python_oracle(connection):
    """Aggregate independent table reads by order; no SQL join/aggregate reuse."""
    orders = {
        order_id
        for order_id, status, timestamp in connection.execute(
            "SELECT order_id,order_status,order_purchase_timestamp FROM olist_orders"
        )
        if status == "delivered" and "2018-07-01" <= timestamp < "2018-08-01"
    }
    items, payments = defaultdict(list), defaultdict(list)
    for order_id, price, freight in connection.execute(
        "SELECT order_id,price,freight_value FROM olist_order_items"
    ):
        items[order_id].append((price, freight))
    for order_id, value in connection.execute(
        "SELECT order_id,payment_value FROM olist_order_payments"
    ):
        payments[order_id].append(value)
    selected = orders & items.keys() & payments.keys()
    product = sum(price for order_id in selected for price, _ in items[order_id])
    freight = sum(value for order_id in selected for _, value in items[order_id])
    payment = sum(value for order_id in selected for value in payments[order_id])
    return {
        "product": [[product]],
        "payment": [[payment]],
        "product_freight": [[product + freight]],
        "both": [[product, payment]],
        "orders": [[len(selected)]],
    }


def rows(connection, sql):
    return [list(row) for row in connection.execute(sql)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=HERE / "OFFLINE_RESULTS.json")
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Output exists; retain it and choose a new output path")
    evidence = []
    with tempfile.TemporaryDirectory(prefix="datatrail-grain-challenge-") as temporary:
        for scenario in ("base", "split_payments", "split_items", "split_both"):
            path = Path(temporary) / (scenario + ".db")
            create_database(path, scenario)
            with closing(sqlite3.connect(path)) as connection:
                oracle = python_oracle(connection)
                assert oracle == {
                    "product": [[50]],
                    "payment": [[58]],
                    "product_freight": [[56]],
                    "both": [[50, 58]],
                    "orders": [[2]],
                }
                for metric, reference in REFERENCES.items():
                    assert rows(connection, reference) == oracle[metric]
                for case in CASES:
                    actual = rows(connection, case["sql"])
                    review = grain_review(case["sql"])
                    evidence.append(
                        {
                            "scenario": scenario,
                            "case_id": case["id"],
                            "category": case["category"],
                            "sql": case["sql"],
                            "expected_rows": oracle[case["metric"]],
                            "actual_rows": actual,
                            "fixture_rows_match": actual == oracle[case["metric"]],
                            "gate_review": review,
                        }
                    )
    controls = [row for row in evidence if row["category"] == "valid_control"]
    allocations = [
        row for row in evidence if row["category"] == "explicit_allocation_control"
    ]
    assert all(row["fixture_rows_match"] for row in controls + allocations)
    report = {
        "tier": "synthetic_sql_mutation_and_valid_controls_not_natural_tasks",
        "model_calls": 0,
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "gate_sha256": hashlib.sha256(
            (ROOT / "packages/dbgpt-app/src/dbgpt_app/commerce_grain.py").read_bytes()
        ).hexdigest(),
        "scenarios": 4,
        "distinct_sql_cases": len(CASES),
        "valid_control_observations": len(controls),
        "valid_controls_reviewed": sum(bool(row["gate_review"]) for row in controls),
        "explicit_allocation_observations": len(allocations),
        "explicit_allocation_reviewed": sum(
            bool(row["gate_review"]) for row in allocations
        ),
        "incorrect_fixture_rows_not_reviewed": [
            {"scenario": row["scenario"], "case_id": row["case_id"]}
            for row in evidence
            if not row["fixture_rows_match"] and not row["gate_review"]
        ],
        "notes": [
            "Artificial fixtures and supplied SQL; "
            "no natural model failure or accuracy gain claim.",
            "Matching one fixture is not proof that a query is semantically correct.",
            "A review is conservative feedback, not a proof that the SQL is wrong.",
            "Both splitting transformations preserve selected "
            "order/product/payment totals.",
            "Valid controls are checked against independent Python "
            "and reference SQL on all four fixtures.",
            "Unqualified columns and partial preaggregation expose "
            "unsupported cases; None is not safe.",
        ],
        "observations": evidence,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "observations"},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
