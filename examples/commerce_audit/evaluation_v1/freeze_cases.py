"""Freeze 8 development and 12 reserved cases before changing the grain gate."""

# Keep the exact SQL/task payloads readable as single fixture values.
# ruff: noqa: E501

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DATABASE = ROOT / "examples/dashboard/olist/data/generated/olist.db"
HERE = Path(__file__).resolve().parent


def amount_case(case_id, split, start, end, *, measure="price", payment=False):
    scope = f"o.order_status='delivered' AND o.order_purchase_timestamp>='{start}' AND o.order_purchase_timestamp<'{end}'"
    exists = (
        " AND EXISTS(SELECT 1 FROM olist_order_payments p WHERE p.order_id=o.order_id)"
        if payment
        else ""
    )
    name = {
        "price": "商品金额（不含运费）",
        "freight_value": "运费",
        "price+freight_value": "含运费成交额",
    }[measure]
    term = "+".join("i." + part for part in measure.split("+"))
    return {
        "id": case_id,
        "split": split,
        "question": f"按下单时间 [{start},{end})、已交付订单，{'只统计至少有一条支付记录的订单，' if payment else ''}统计{name}和有商品记录的订单去重数。结果每行依次为[金额,订单数]；金额保留两位，空集合金额保持NULL，订单数为0。",
        "reference_sql": f"SELECT ROUND(SUM({term}),2),COUNT(DISTINCT o.order_id) FROM olist_orders o JOIN olist_order_items i ON i.order_id=o.order_id WHERE {scope}{exists}",
        "alternate_sql": f"WITH x AS (SELECT order_id,SUM({measure}) amount FROM olist_order_items GROUP BY order_id) SELECT ROUND(SUM(x.amount),2),COUNT(*) FROM olist_orders o JOIN x ON x.order_id=o.order_id WHERE {scope}{exists}",
    }


def payment_case(case_id, split, start, end):
    scope = f"o.order_status='delivered' AND o.order_purchase_timestamp>='{start}' AND o.order_purchase_timestamp<'{end}'"
    return {
        "id": case_id,
        "split": split,
        "question": f"按下单时间 [{start},{end}) 的已交付订单，只统计至少包含一条商品明细的订单。统计支付总额和有支付记录的订单去重数，每行依次为[支付金额,订单数]，金额保留两位。",
        "reference_sql": f"SELECT ROUND(SUM(p.payment_value),2),COUNT(DISTINCT o.order_id) FROM olist_orders o JOIN olist_order_payments p ON p.order_id=o.order_id WHERE {scope} AND EXISTS(SELECT 1 FROM olist_order_items i WHERE i.order_id=o.order_id)",
        "alternate_sql": f"WITH p AS (SELECT order_id,SUM(payment_value) amount FROM olist_order_payments GROUP BY order_id) SELECT ROUND(SUM(p.amount),2),COUNT(*) FROM p JOIN olist_orders o ON o.order_id=p.order_id WHERE {scope} AND o.order_id IN(SELECT order_id FROM olist_order_items)",
    }


def main():
    output = HERE / "cases.json"
    if output.exists():
        raise SystemExit("Cases already frozen; do not overwrite after development")
    cases = [
        amount_case("D01", "development", "2018-07-01", "2018-08-01"),
        amount_case("D02", "development", "2018-07-01", "2018-08-01", payment=True),
        payment_case("D03", "development", "2018-07-01", "2018-08-01"),
        amount_case(
            "D04",
            "development",
            "2018-07-01",
            "2018-08-01",
            measure="price+freight_value",
            payment=True,
        ),
        amount_case("D05", "development", "2016-01-01", "2019-01-01", payment=True),
        amount_case("D06", "development", "2019-01-01", "2019-02-01"),
        {
            "id": "D07",
            "split": "development",
            "question": "统计2018年7月下单、状态为canceled的订单数，只查订单表，不要求有商品或支付记录。每行只输出[订单数]。",
            "reference_sql": "SELECT COUNT(*) FROM olist_orders WHERE order_status='canceled' AND order_purchase_timestamp>='2018-07-01' AND order_purchase_timestamp<'2018-08-01'",
            "alternate_sql": "SELECT COALESCE(SUM(CASE WHEN order_status='canceled' AND substr(order_purchase_timestamp,1,7)='2018-07' THEN 1 ELSE 0 END),0) FROM olist_orders",
        },
        {
            "id": "D08",
            "split": "development",
            "question": "计算2018年7月已交付订单的优惠券减免总金额。若当前数据没有明确的优惠券减免字段，不得用支付金额差推断，请返回unsupported。",
            "unsupported": True,
        },
        amount_case("H01", "reserved", "2017-07-01", "2017-08-01", payment=True),
        amount_case("H02", "reserved", "2018-08-01", "2018-09-01"),
        payment_case("H03", "reserved", "2017-09-01", "2017-10-01"),
        amount_case(
            "H04",
            "reserved",
            "2018-06-01",
            "2018-07-01",
            measure="freight_value",
            payment=True,
        ),
        {
            "id": "H05",
            "split": "reserved",
            "question": "2018年7月已交付、同时有商品和支付记录的订单去重数。可以原始关联这两张明细，但只数不同订单、不累计金额。每行输出[订单数]。",
            "reference_sql": "SELECT COUNT(DISTINCT o.order_id) FROM olist_orders o JOIN olist_order_items i ON o.order_id=i.order_id JOIN olist_order_payments p ON o.order_id=p.order_id WHERE o.order_status='delivered' AND o.order_purchase_timestamp>='2018-07-01' AND o.order_purchase_timestamp<'2018-08-01'",
            "alternate_sql": "SELECT COUNT(*) FROM olist_orders o WHERE order_status='delivered' AND substr(order_purchase_timestamp,1,7)='2018-07' AND EXISTS(SELECT 1 FROM olist_order_items i WHERE i.order_id=o.order_id) AND EXISTS(SELECT 1 FROM olist_order_payments p WHERE p.order_id=o.order_id)",
        },
        {
            "id": "H06",
            "split": "reserved",
            "question": "统计2018年7月下单的已交付订单，按支付方式汇总支付金额，金额保留两位，按payment_type升序，每行[支付方式,支付金额]。",
            "reference_sql": "SELECT p.payment_type,ROUND(SUM(p.payment_value),2) FROM olist_order_payments p JOIN olist_orders o ON o.order_id=p.order_id WHERE o.order_status='delivered' AND substr(o.order_purchase_timestamp,1,7)='2018-07' GROUP BY p.payment_type ORDER BY p.payment_type",
            "alternate_sql": "WITH p AS(SELECT order_id,payment_type,SUM(payment_value) amount FROM olist_order_payments GROUP BY order_id,payment_type) SELECT p.payment_type,ROUND(SUM(p.amount),2) FROM p JOIN olist_orders o ON o.order_id=p.order_id WHERE o.order_status='delivered' AND o.order_purchase_timestamp>='2018-07-01' AND o.order_purchase_timestamp<'2018-08-01' GROUP BY p.payment_type ORDER BY p.payment_type",
        },
        {
            "id": "H07",
            "split": "reserved",
            "question": "2018年7月已交付订单，按客户收货州统计SP与RJ的商品金额（不含运费），保留两位，州升序。每行[州,金额]。",
            "reference_sql": "SELECT c.customer_state,ROUND(SUM(i.price),2) FROM olist_orders o JOIN olist_customers c ON c.customer_id=o.customer_id JOIN olist_order_items i ON i.order_id=o.order_id WHERE o.order_status='delivered' AND substr(o.order_purchase_timestamp,1,7)='2018-07' AND c.customer_state IN('SP','RJ') GROUP BY c.customer_state ORDER BY c.customer_state",
            "alternate_sql": "SELECT c.customer_state,ROUND(SUM((SELECT SUM(i.price) FROM olist_order_items i WHERE i.order_id=o.order_id)),2) FROM olist_orders o JOIN olist_customers c ON c.customer_id=o.customer_id WHERE o.order_status='delivered' AND o.order_purchase_timestamp>='2018-07-01' AND o.order_purchase_timestamp<'2018-08-01' AND c.customer_state IN('SP','RJ') GROUP BY c.customer_state ORDER BY c.customer_state",
        },
        {
            "id": "H08",
            "split": "reserved",
            "question": "2018年7月已交付且有商品明细的订单，计算商品金额客单价（不含运费），总商品金额除以订单去重数，保留两位。每行[客单价]。",
            "reference_sql": "SELECT ROUND(SUM(i.price)/COUNT(DISTINCT o.order_id),2) FROM olist_orders o JOIN olist_order_items i ON i.order_id=o.order_id WHERE o.order_status='delivered' AND substr(o.order_purchase_timestamp,1,7)='2018-07'",
            "alternate_sql": "WITH i AS(SELECT order_id,SUM(price) amount FROM olist_order_items GROUP BY order_id) SELECT ROUND(AVG(i.amount),2) FROM i JOIN olist_orders o ON i.order_id=o.order_id WHERE o.order_status='delivered' AND o.order_purchase_timestamp>='2018-07-01' AND o.order_purchase_timestamp<'2018-08-01'",
        },
        amount_case("H09", "reserved", "2018-07-31", "2018-08-01", payment=True),
        amount_case("H10", "reserved", "2015-01-01", "2015-02-01"),
        amount_case(
            "H11",
            "reserved",
            "2017-11-01",
            "2017-12-01",
            measure="price+freight_value",
            payment=True,
        ),
        {
            "id": "H12",
            "split": "reserved",
            "question": "2018年7月已交付、有商品和支付记录、支付合计大于0的订单，按每笔支付额占该订单支付总额的比例分摊商品金额，再汇总全部分摊额。这里明确允许按订单内支付比例分摊；每行[分摊总金额]，保留两位。",
            "reference_sql": "WITH p AS(SELECT order_id,SUM(payment_value) total FROM olist_order_payments GROUP BY order_id) SELECT ROUND(SUM(i.price),2) FROM olist_orders o JOIN olist_order_items i ON i.order_id=o.order_id JOIN p ON p.order_id=o.order_id WHERE o.order_status='delivered' AND substr(o.order_purchase_timestamp,1,7)='2018-07' AND p.total>0",
            "alternate_sql": "WITH pt AS(SELECT order_id,SUM(payment_value) total FROM olist_order_payments GROUP BY order_id) SELECT ROUND(SUM(i.price*p.payment_value/pt.total),2) FROM olist_orders o JOIN olist_order_items i ON i.order_id=o.order_id JOIN olist_order_payments p ON p.order_id=o.order_id JOIN pt ON pt.order_id=o.order_id WHERE o.order_status='delivered' AND o.order_purchase_timestamp>='2018-07-01' AND o.order_purchase_timestamp<'2018-08-01' AND pt.total>0",
        },
    ]
    with sqlite3.connect(DATABASE.as_uri() + "?mode=ro", uri=True) as connection:
        schema = "\n".join(
            row[0]
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name LIKE 'olist_%' ORDER BY name"
            )
            if row[0]
        )
        for case in cases:
            print("Checking independent SQL: " + case["id"], flush=True)
            if case.get("unsupported"):
                case["expected_rows"] = []
                continue
            deadline = time.monotonic() + 15
            connection.set_progress_handler(
                lambda: int(time.monotonic() > deadline), 10000
            )
            first = connection.execute(case["reference_sql"]).fetchall()
            deadline = time.monotonic() + 15
            second = connection.execute(case["alternate_sql"]).fetchall()
            if first != second:
                raise AssertionError((case["id"], first, second))
            case["expected_rows"] = first
    output.write_text(
        json.dumps(
            {
                "version": 1,
                "frozen_at": datetime.now(timezone.utc).isoformat(),
                "baseline_commit": "1ac64e358f212bd80600f7afa0643bedf9ef8f41",
                "database_sha256": hashlib.sha256(DATABASE.read_bytes()).hexdigest(),
                "schema": schema,
                "cases": cases,
                "limits": "Reserved cases sealed before gate changes; same author curates both splits. Not blinded, externally authored, or a permanent unseen set after this run.",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "cases": len(cases),
                "development": 8,
                "reserved": 12,
                "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
