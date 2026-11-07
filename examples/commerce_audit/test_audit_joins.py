import sqlite3

import pytest

from examples.commerce_audit.audit_joins import diagnose


def make_database(prefix=""):
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        f"CREATE TABLE {prefix}orders(order_id TEXT, order_status TEXT);"
        f"CREATE TABLE {prefix}order_items(order_id TEXT, price REAL);"
        f"CREATE TABLE {prefix}order_payments(order_id TEXT, payment_value REAL);"
    )
    return connection


@pytest.mark.parametrize("prefix", ["", "olist_"])
def test_payment_fanout_and_equal_priced_items(prefix):
    with make_database(prefix) as connection:
        connection.executescript(
            f"INSERT INTO {prefix}orders VALUES ('a','delivered');"
            f"INSERT INTO {prefix}order_items VALUES ('a',10),('a',10);"
            f"INSERT INTO {prefix}order_payments VALUES ('a',5),('a',15);"
        )
        result = diagnose(connection)
        assert result["item_rows_before_payment_join"] == 2
        assert result["item_rows_after_payment_join"] == 4
        assert result["item_amount_before_payment_join"] == 20
        assert result["item_amount_after_naive_payment_join"] == 40
        assert result["orders_with_multiple_items_and_payments"] == 1
        # SUM(DISTINCT price) would incorrectly return 10 for these valid items.


def test_same_scope_excludes_missing_payments_and_cancelled_orders():
    with make_database() as connection:
        connection.executescript(
            "INSERT INTO orders VALUES "
            "('a','delivered'),('b','delivered'),('c','canceled');"
            "INSERT INTO order_items VALUES ('a',10),('b',100),('c',1000);"
            "INSERT INTO order_payments VALUES ('a',10),('c',1000);"
        )
        result = diagnose(connection)
        assert result["matched_order_count"] == 1
        assert result["item_amount_before_payment_join"] == 10
        assert result["item_amount_after_naive_payment_join"] == 10
        assert result["orders_whose_item_rows_repeat"] == 0


def test_empty_scope_is_not_reported_as_a_zero_amount():
    with make_database() as connection:
        result = diagnose(connection)
        assert result["matched_order_count"] == 0
        assert result["item_rows_after_payment_join"] == 0
        assert result["item_amount_before_payment_join"] is None
        assert result["item_amount_after_naive_payment_join"] is None


def test_missing_schema_fails_explicitly():
    with sqlite3.connect(":memory:") as connection:
        with pytest.raises(ValueError, match="tables are missing"):
            diagnose(connection)
