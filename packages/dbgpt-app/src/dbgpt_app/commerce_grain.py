"""Conservative review gate for raw Olist item/payment aggregation.

This recognizes a narrow AST pattern. It does not prove arbitrary SQL correct.
Preaggregated subqueries, CTEs and intentional allocation require other checks.
"""

from typing import Optional

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope


def grain_review(sql: str, dialect: str = "sqlite") -> Optional[dict]:
    tree = sqlglot.parse_one(sql, read=dialect)
    for scope in traverse_scope(tree):
        raw_tables = {
            alias: source.name.lower()
            for alias, (_, source) in scope.selected_sources.items()
            if isinstance(source, exp.Table)
        }
        for prefix in ("olist_", ""):
            items = {a for a, t in raw_tables.items() if t == prefix + "order_items"}
            payments = {
                a for a, t in raw_tables.items() if t == prefix + "order_payments"
            }
            if not items or not payments:
                continue
            # Scope.columns excludes columns belonging to nested subqueries.
            for column in scope.columns:
                if column.find_ancestor(exp.Select) is not scope.expression:
                    continue
                aggregate = column.find_ancestor(exp.Sum)
                if aggregate is None:
                    continue
                value = aggregate.this
                if isinstance(value, exp.Distinct) and len(value.expressions) == 1:
                    value = value.expressions[0]
                # Include amount columns wrapped in arithmetic, COALESCE or
                # CAST, but never borrow columns from a nested SELECT scope.
                if not any(node is column for node in value.walk()):
                    continue
                item_amount = column.table in items and column.name.lower() in {
                    "price",
                    "freight_value",
                }
                payment_amount = (
                    column.table in payments and column.name.lower() == "payment_value"
                )
                if item_amount or payment_amount:
                    return {
                        "code": "COMMERCE_GRAIN_REVIEW_REQUIRED",
                        "expression": aggregate.sql(),
                        "reason": "商品与支付明细直接关联后累计金额，可能重复计数。",
                        "next_action": (
                            "先明确指标粒度。按 order_id 分别预聚合后关联，"
                            "或仅为筛选支付存在性使用 EXISTS；"
                            "不要用 SUM(DISTINCT 金额) 去重。若需要按"
                            "支付方式分摊商品金额，请先询问分摊口径。该规则要求复核，"
                            "不表示所有多对多关联都错误。"
                        ),
                    }
    return None
