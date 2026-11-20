"""Conservative review gate for fan-out joins declared in a metric contract.

This recognizes narrow AST patterns: a contract amount column, or a CTE/subquery
output derived from one, summed in the same SELECT scope as raw detail rows that
repeat it. It does not prove arbitrary SQL correct. Intentional allocation still
requires a human-confirmed metric definition.
"""

from typing import TYPE_CHECKING, Optional

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

if TYPE_CHECKING:
    from dbgpt_app.commerce_contract import GrainRule, MetricContract


def _derived_amount_outputs(
    source: Scope, table_names: set[str], amount_cols: set[str], allow_unqualified: bool
) -> set[str]:
    """Output names of a CTE/subquery whose values come from a contract amount."""
    if not amount_cols or not isinstance(source.expression, exp.Select):
        return set()
    raw = {
        alias.lower(): table.name.lower()
        for alias, (_, table) in source.selected_sources.items()
        if isinstance(table, exp.Table)
    }
    aliases = {alias for alias, table in raw.items() if table in table_names}
    if not aliases:
        return set()
    # An unqualified column is unambiguous when the subquery reads a single table.
    unqualified_ok = allow_unqualified or len(source.selected_sources) == 1
    outputs = set()
    for select in source.expression.selects:
        for column in select.find_all(exp.Column):
            owner = column.table.lower()
            if column.name.lower() in amount_cols and (
                owner in aliases or (not owner and unqualified_ok)
            ):
                outputs.add(select.alias_or_name.lower())
                break
    return outputs


def _one_row_tables(scope, raw_tables: dict[str, str], rule: "GrainRule") -> set[str]:
    """Tables reduced to one row per key by a declared filter in WHERE or JOIN ON."""
    declared = {
        (f.table.lower(), f.column.lower()): str(f.value) for f in rule.one_row_filters
    }
    if not declared:
        return set()
    select = scope.expression
    conditions = [select.args.get("where")] + [
        join.args.get("on") for join in select.args.get("joins") or []
    ]
    found = set()
    for condition in filter(None, conditions):
        for eq in condition.find_all(exp.EQ):
            # Only trust filters that are plain AND conditions of this SELECT.
            if eq.find_ancestor(exp.Or, exp.Not, exp.Select) is not select:
                continue
            column, literal = eq.this, eq.expression
            if isinstance(column, exp.Literal):
                column, literal = literal, column
            if not isinstance(column, exp.Column) or not isinstance(
                literal, exp.Literal
            ):
                continue
            table = raw_tables.get(column.table.lower())
            if table and declared.get((table, column.name.lower())) == literal.name:
                found.add(table)
    return found


def _match_rule(scope, raw_tables: dict[str, str], rule: "GrainRule") -> Optional[dict]:
    left_names = {t.lower() for t in rule.left_tables}
    right_names = {t.lower() for t in rule.right_tables}
    left_aliases = {a for a, t in raw_tables.items() if t in left_names}
    right_aliases = {a for a, t in raw_tables.items() if t in right_names}
    # Detail tables limited to one row per key no longer repeat the other side.
    one_row = _one_row_tables(scope, raw_tables, rule)
    left_multipliers = {a for a in left_aliases if raw_tables[a] not in one_row}
    right_multipliers = {a for a in right_aliases if raw_tables[a] not in one_row}
    left_cols = {c.lower() for c in rule.left_amount_columns}
    right_cols = {c.lower() for c in rule.right_amount_columns}
    # Amounts preaggregated in a CTE/subquery still repeat next to raw detail rows.
    left_derived: dict[str, set[str]] = {}
    right_derived: dict[str, set[str]] = {}
    for alias, (_, source) in scope.selected_sources.items():
        if isinstance(source, Scope):
            name = alias.lower()
            left_derived[name] = _derived_amount_outputs(
                source, left_names, left_cols, rule.allow_unqualified
            )
            right_derived[name] = _derived_amount_outputs(
                source, right_names, right_cols, rule.allow_unqualified
            )
    # Left amounts are duplicated by raw right rows, and vice versa.
    left_at_risk = bool(right_multipliers) and (
        bool(left_aliases) or any(left_derived.values())
    )
    right_at_risk = bool(left_multipliers) and (
        bool(right_aliases) or any(right_derived.values())
    )
    if not left_at_risk and not right_at_risk:
        return None
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
        hit_left = left_at_risk and (
            (
                (tbl in left_aliases or (rule.allow_unqualified and not tbl))
                and col_name in left_cols
            )
            or col_name in left_derived.get(tbl, set())
        )
        hit_right = right_at_risk and (
            (
                (tbl in right_aliases or (rule.allow_unqualified and not tbl))
                and col_name in right_cols
            )
            or col_name in right_derived.get(tbl, set())
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
    if contract is None:
        # Without a database-specific contract, keep the original Olist rules.
        from dbgpt_app.commerce_contract import load_contract

        contract = load_contract("olist")
    tree = sqlglot.parse_one(sql, read=dialect)
    for scope in traverse_scope(tree):
        raw_tables = {
            alias.lower() if isinstance(alias, str) else alias: source.name.lower()
            for alias, (_, source) in scope.selected_sources.items()
            if isinstance(source, exp.Table)
        }
        for rule in contract.grain_rules:
            hit = _match_rule(scope, raw_tables, rule)
            if hit is not None:
                return hit
    return None
