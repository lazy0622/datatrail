import pytest

from dbgpt_app.commerce_grain import grain_review


@pytest.mark.parametrize("prefix", ["", "olist_"])
@pytest.mark.parametrize("measure", ["i.price", "p.payment_value", "i.freight_value"])
def test_raw_child_amount_requires_review(prefix, measure):
    sql = f"""SELECT SUM({measure}) FROM {prefix}order_items i
    JOIN {prefix}order_payments p ON p.order_id=i.order_id"""
    assert grain_review(sql)["code"] == "COMMERCE_GRAIN_REVIEW_REQUIRED"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SUM(i.price) FROM order_items i WHERE EXISTS "
        "(SELECT 1 FROM order_payments p WHERE p.order_id=i.order_id)",
        "WITH p AS (SELECT order_id, SUM(payment_value) AS amount "
        "FROM order_payments GROUP BY order_id) "
        "SELECT SUM(i.price) FROM order_items i JOIN p ON p.order_id=i.order_id",
        "SELECT COUNT(DISTINCT i.order_id) FROM order_items i "
        "JOIN order_payments p ON p.order_id=i.order_id",
        "SELECT SUM(i.price) FROM unrelated_items i JOIN unrelated_payments p ON 1=1",
        "SELECT SUM(price) FROM order_items",
    ],
)
def test_outside_supported_risk_pattern_is_not_rejected(sql):
    assert grain_review(sql) is None


def test_distinct_amount_is_not_a_safe_way_to_remove_join_duplicates():
    assert grain_review(
        "SELECT SUM(DISTINCT i.price) FROM order_items i "
        "JOIN order_payments p ON p.order_id=i.order_id"
    )["code"] == "COMMERCE_GRAIN_REVIEW_REQUIRED"
