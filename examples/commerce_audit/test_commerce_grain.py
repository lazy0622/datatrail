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
    assert (
        grain_review(
            "SELECT SUM(DISTINCT i.price) FROM order_items i "
            "JOIN order_payments p ON p.order_id=i.order_id"
        )["code"]
        == "COMMERCE_GRAIN_REVIEW_REQUIRED"
    )


@pytest.mark.parametrize(
    "expression",
    [
        "i.price+i.freight_value",
        "i.price*1.0",
        "COALESCE(p.payment_value,0)",
        "CAST(i.price AS REAL)",
        "ROUND(i.freight_value,2)",
        "DISTINCT (i.price+i.freight_value)",
    ],
)
def test_amount_inside_sum_expression_requires_review(expression):
    result = grain_review(
        f"SELECT SUM({expression}) FROM order_items i "
        "JOIN order_payments p ON p.order_id=i.order_id"
    )
    assert result["code"] == "COMMERCE_GRAIN_REVIEW_REQUIRED"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(DISTINCT i.order_id) FROM order_items i "
        "JOIN order_payments p ON p.order_id=i.order_id",
        "SELECT SUM((SELECT MAX(i.price) FROM other_table x)) "
        "FROM order_items i JOIN order_payments p ON p.order_id=i.order_id",
        "WITH i AS (SELECT order_id,SUM(price) amount FROM order_items "
        "GROUP BY order_id), "
        "p AS (SELECT order_id,SUM(payment_value) paid FROM order_payments "
        "GROUP BY order_id) "
        "SELECT SUM(i.amount),SUM(p.paid) FROM i JOIN p ON p.order_id=i.order_id",
    ],
)
def test_expression_review_keeps_scope_boundaries(sql):
    assert grain_review(sql) is None


@pytest.mark.parametrize(
    "sql",
    [
        # Unqualified amount column next to a raw payment join.
        "SELECT SUM(price) FROM order_items i "
        "JOIN order_payments p ON p.order_id=i.order_id",
        # Items preaggregated per order, then repeated by raw payment rows.
        "SELECT SUM(x.amount) FROM "
        "(SELECT order_id,SUM(price+freight_value) amount FROM order_items "
        "GROUP BY order_id) x JOIN order_payments p ON p.order_id=x.order_id",
        "WITH i AS (SELECT order_id,SUM(price) amount FROM olist_order_items "
        "GROUP BY order_id) SELECT SUM(i.amount) FROM i "
        "JOIN olist_order_payments p ON p.order_id=i.order_id",
    ],
)
def test_partially_preaggregated_or_unqualified_amount_requires_review(sql):
    assert grain_review(sql)["code"] == "COMMERCE_GRAIN_REVIEW_REQUIRED"


RAW_JOIN = "FROM order_items i JOIN order_payments p ON p.order_id=i.order_id"


def test_declared_one_row_filter_clears_only_the_side_it_protects():
    # One payment row per order: item amounts are no longer repeated.
    assert (
        grain_review(f"SELECT SUM(i.price) {RAW_JOIN} WHERE p.payment_sequential=1")
        is None
    )
    assert (
        grain_review(
            "SELECT SUM(i.price) FROM order_items i JOIN order_payments p "
            "ON p.order_id=i.order_id AND p.payment_sequential=1"
        )
        is None
    )
    # Payment amounts are still repeated by several item rows.
    assert grain_review(
        f"SELECT SUM(p.payment_value) {RAW_JOIN} WHERE p.payment_sequential=1"
    )


@pytest.mark.parametrize(
    "where",
    [
        "p.payment_sequential=2",
        "p.payment_sequential=1 OR i.price>0",
        "NOT p.payment_sequential=1",
        "i.order_id IN (SELECT order_id FROM order_payments q "
        "WHERE q.payment_sequential=1)",
    ],
)
def test_one_row_filter_must_be_a_plain_matching_condition(where):
    assert grain_review(f"SELECT SUM(i.price) {RAW_JOIN} WHERE {where}")
