"""Conservative review gate for raw Olist item/payment aggregation.

This recognizes a narrow AST pattern. It does not prove arbitrary SQL correct.
Preaggregated subqueries, CTEs and intentional allocation require other checks.
"""

from typing import TYPE_CHECKING, Optional

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

if TYPE_CHECKING:
    from dbgpt_app.commerce_contract import GrainRule, MetricContract


def _match_rule(scope, raw_tables: dict[str, str], rule: "GrainRule") -> Optional[dict]:
    left_names = {t.lower() for t in rule.left_tables}
    right_names = {t.lower() for t in rule.right_tables}
    left_aliases = {a for a, t in raw_tables.items() if t in left_names}
    right_aliases = {a for a, t in raw_tables.items() if t in right_names}
    if not left_aliases or not right_aliases:
        return None
    left_cols = {c.lower() for c in rule.left_amount_columns}
    right_cols = {c.lower() for c in rule.right_amount_columns}
    for column in scope.columns:
        if column.find_ancestor(exp.Select) is not scope.expression:
            continue
        aggregate = column.find_ancestor(exp.Sum)
        if aggregate is None:
            continue
        value = aggregate.this
        if isinstance(value, exp.Distinct) and len(value.expressions) == 1:
            value = value.expressions[0]
        if not any(node is column for node in value.walk()):
            continue
        col_name = column.name.lower()
        tbl = column.table.lower()
        hit_left = (tbl in left_aliases or (rule.allow_unqualified and not tbl)) and (
            col_name in left_cols
        )
        hit_right = (tbl in right_aliases or (rule.allow_unqualified and not tbl)) and (
            col_name in right_cols
        )
        if hit_left or hit_right:
            return {
                "code": "COMMERCE_GRAIN_REVIEW_REQUIRED",
                "rule_id": rule.id,
                "expression": aggregate.sql(),
                "reason": rule.guidance,
                "next_action": rule.guidance,
            }
    return None


def grain_review(
    sql: str,
    dialect: str = "sqlite",
    contract: Optional["MetricContract"] = None,
) -> Optional[dict]:
    tree = sqlglot.parse_one(sql, read=dialect)
    for scope in traverse_scope(tree):
        raw_tables = {
            alias.lower() if isinstance(alias, str) else alias: source.name.lower()
            for alias, (_, source) in scope.selected_sources.items()
            if isinstance(source, exp.Table)
        }
        if contract is not None:
            for rule in contract.grain_rules:
                hit = _match_rule(scope, raw_tables, rule)
                if hit is not None:
                    return hit
            continue
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
                tbl = column.table.lower()
                item_amount = tbl in items and column.name.lower() in {
                    "price",
                    "freight_value",
                }
                payment_amount = (
                    tbl in payments and column.name.lower() == "payment_value"
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
