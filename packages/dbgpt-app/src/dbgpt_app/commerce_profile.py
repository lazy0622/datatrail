"""DataTrail read-only Olist join profiling."""

import sqlite3


def diagnose(connection: sqlite3.Connection) -> dict:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if {"olist_orders", "olist_order_items", "olist_order_payments"} <= tables:
        prefix = "olist_"
        dataset_kind = "public_data_requires_source_manifest"
    elif {"orders", "order_items", "order_payments"} <= tables:
        prefix = ""
        dataset_kind = "unspecified"
    else:
        raise ValueError("Required order, item and payment tables are missing.")
    metadata = {}
    if "dataset_metadata" in tables:
        metadata = dict(connection.execute("SELECT key, value FROM dataset_metadata"))
        dataset_kind = metadata.get("dataset_kind", dataset_kind)

    # Names are selected from the two fixed schemas above, never model input.
    orders = prefix + "orders"
    items = prefix + "order_items"
    payments = prefix + "order_payments"
    profile = connection.execute(
        f"""
        WITH i AS (
            SELECT order_id, COUNT(*) AS n FROM {items} GROUP BY order_id
        ), p AS (
            SELECT order_id, COUNT(*) AS n FROM {payments} GROUP BY order_id
        )
        SELECT COUNT(*), COALESCE(SUM(i.n), 0), COALESCE(SUM(i.n*p.n), 0),
               COALESCE(SUM(CASE WHEN i.n>1 AND p.n>1 THEN 1 ELSE 0 END), 0),
               COALESCE(SUM(CASE WHEN p.n>1 THEN 1 ELSE 0 END), 0)
        FROM {orders} o JOIN i ON i.order_id=o.order_id
        JOIN p ON p.order_id=o.order_id
        WHERE o.order_status='delivered'
        """
    ).fetchone()
    safe_total = connection.execute(
        f"""
        SELECT ROUND(SUM(i.price), 2) FROM {orders} o
        JOIN {items} i ON i.order_id=o.order_id
        WHERE o.order_status='delivered'
        AND EXISTS (SELECT 1 FROM {payments} p WHERE p.order_id=o.order_id)
        """
    ).fetchone()[0]
    joined_total = connection.execute(
        f"""
        SELECT ROUND(SUM(i.price), 2) FROM {orders} o
        JOIN {items} i ON i.order_id=o.order_id
        JOIN {payments} p ON p.order_id=o.order_id
        WHERE o.order_status='delivered'
        """
    ).fetchone()[0]
    return {
        "evaluation_tier": "data_diagnostic_not_model_performance",
        "dataset_kind": dataset_kind,
        "dataset_metadata": metadata,
        "table_prefix": prefix,
        "scope": "delivered orders having both item and payment records",
        "matched_order_count": profile[0],
        "item_rows_before_payment_join": profile[1],
        "item_rows_after_payment_join": profile[2],
        "orders_with_multiple_items_and_payments": profile[3],
        "orders_whose_item_rows_repeat": profile[4],
        "item_amount_before_payment_join": safe_total,
        "item_amount_after_naive_payment_join": joined_total,
        "interpretation": (
            "Payment joins can repeat item-grain amounts. This is an illustrative "
            "SQL counterexample, not evidence that the upstream model makes it. "
            "A many-to-many relationship is not itself an invalid business query."
        ),
    }
