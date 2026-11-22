"""Cross-session metric-definition memory (口径记忆) for DataTrail.

The model may only *propose* a definition. A definition is applied to later
sessions after its owner confirms it, and only for the same owner and database.
Every record keeps where it came from and can be revoked. A definition that
contradicts the dataset's MetricContract is never applied; the contract wins and
the conflict is shown instead.
"""

import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from dbgpt_app.commerce_contract import MetricContract

# Column names only; the live model once wrote "column（说明…）" into this field.
_COLUMN = r"^[A-Za-z_][A-Za-z0-9_.]*$"


class MetricDefinition(BaseModel):
    """One business metric definition, structured so it can be checked."""

    model_config = ConfigDict(extra="forbid")
    metric: str = Field(min_length=1, max_length=80)
    expression: str = Field(min_length=1, max_length=500)
    time_field: Optional[str] = Field(default=None, max_length=120, pattern=_COLUMN)
    filters: list[str] = Field(default_factory=list, max_length=10)
    currency: Optional[str] = Field(default=None, pattern=r"^[A-Za-z]{3}$")
    note: str = Field(default="", max_length=300)


class MetricMemoryError(ValueError):
    """A memory operation that must not proceed (missing, foreign or conflicting)."""


def contract_conflicts(
    definition: MetricDefinition, contract: Optional[MetricContract]
) -> list[str]:
    """Return reasons the definition contradicts what the contract declares."""
    if contract is None:
        return []
    reasons = []
    if (
        definition.currency
        and contract.currency
        and definition.currency.upper() != contract.currency.upper()
    ):
        reasons.append(
            f"币种 {definition.currency} 与契约币种 {contract.currency} 不一致"
        )
    spec = contract.time_contract
    if definition.time_field and spec is not None:
        known = {spec.default_field, *spec.fields.values(), *spec.other_time_fields}
        if definition.time_field not in known:
            reasons.append(f"时间字段 {definition.time_field} 不是契约声明的时间字段")
    return reasons


class MetricMemory:
    """SQLite-backed memory keyed by owner × database × metric."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS metric_memory ("
                "id INTEGER PRIMARY KEY, owner TEXT NOT NULL, database TEXT NOT NULL,"
                "metric TEXT NOT NULL, definition TEXT NOT NULL, status TEXT NOT NULL,"
                "source_conv TEXT NOT NULL, source_question TEXT NOT NULL,"
                "created_at TEXT NOT NULL, confirmed_at TEXT, revoked_at TEXT)"
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _record(row: sqlite3.Row) -> dict:
        return {**dict(row), "definition": json.loads(row["definition"])}

    def propose(
        self,
        owner: str,
        database: str,
        definition: MetricDefinition,
        source_conv: str,
        source_question: str,
    ) -> dict:
        """Store a model-proposed definition; it has no effect until confirmed."""
        if not owner.strip():
            raise MetricMemoryError("匿名会话不能写入口径记忆")
        with closing(self._connect()) as db, db:
            cursor = db.execute(
                "INSERT INTO metric_memory(owner,database,metric,definition,status,"
                "source_conv,source_question,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    owner,
                    database,
                    definition.metric,
                    definition.model_dump_json(),
                    "proposed",
                    source_conv,
                    source_question[:500],
                    datetime.now(UTC).isoformat(),
                ),
            )
            row = db.execute(
                "SELECT * FROM metric_memory WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
        return self._record(row)

    def _owned(self, db: sqlite3.Connection, memory_id: int, owner: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM metric_memory WHERE id=? AND owner=?", (memory_id, owner)
        ).fetchone()
        if row is None:
            raise MetricMemoryError("口径记忆不存在")
        return row

    def get(self, memory_id: int, owner: str) -> dict:
        """Return one of the owner's records; other owners' records look absent."""
        with closing(self._connect()) as db:
            return self._record(self._owned(db, memory_id, owner))

    def confirm(
        self, memory_id: int, owner: str, contract: Optional[MetricContract]
    ) -> dict:
        """Confirm a proposal; refuse on contract conflict; supersede the old one."""
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = self._owned(db, memory_id, owner)
                if row["status"] != "proposed":
                    raise MetricMemoryError("只有待确认的口径可以确认")
                definition = MetricDefinition.model_validate_json(row["definition"])
                reasons = contract_conflicts(definition, contract)
                if reasons:
                    raise MetricMemoryError("与指标契约冲突：" + "；".join(reasons))
                now = datetime.now(UTC).isoformat()
                db.execute(
                    "UPDATE metric_memory SET status='superseded', revoked_at=? "
                    "WHERE owner=? AND database=? AND metric=? AND status='confirmed'",
                    (now, owner, row["database"], row["metric"]),
                )
                db.execute(
                    "UPDATE metric_memory SET status='confirmed', confirmed_at=? "
                    "WHERE id=?",
                    (now, memory_id),
                )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
            return self._record(self._owned(db, memory_id, owner))

    def revoke(self, memory_id: int, owner: str) -> dict:
        """Stop applying a definition; the record stays for provenance."""
        with closing(self._connect()) as db, db:
            row = self._owned(db, memory_id, owner)
            if row["status"] not in {"proposed", "confirmed"}:
                raise MetricMemoryError("该口径已失效")
            db.execute(
                "UPDATE metric_memory SET status='revoked', revoked_at=? WHERE id=?",
                (datetime.now(UTC).isoformat(), memory_id),
            )
            return self._record(self._owned(db, memory_id, owner))

    def records(self, owner: str, database: str, status: Optional[str] = None) -> list:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM metric_memory WHERE owner=? AND database=? "
                "AND (? IS NULL OR status=?) ORDER BY id",
                (owner, database, status, status),
            ).fetchall()
        return [self._record(row) for row in rows]

    def prompt(
        self, owner: str, database: str, contract: Optional[MetricContract]
    ) -> str:
        """Render confirmed definitions for SQL generation; list conflicts apart."""
        if not owner.strip():
            return ""
        applied, blocked = [], []
        for record in self.records(owner, database, "confirmed"):
            definition = MetricDefinition.model_validate(record["definition"])
            reasons = contract_conflicts(definition, contract)
            line = (
                f"- {definition.metric} = {definition.expression}"
                + (
                    f"；时间字段 {definition.time_field}"
                    if definition.time_field
                    else ""
                )
                + (
                    f"；过滤 {' AND '.join(definition.filters)}"
                    if definition.filters
                    else ""
                )
                + (f"；币种 {definition.currency}" if definition.currency else "")
                + f"（记忆 #{record['id']}，确认于 {record['confirmed_at'][:10]}，"
                f"来源会话 {record['source_conv']}）"
            )
            (blocked if reasons else applied).append((line, reasons))
        if not applied and not blocked:
            return ""
        lines = ["## 用户已确认的指标口径（跨会话记忆）"]
        if applied:
            lines.append(
                "用户问到下列指标时按此口径生成 SQL，并在回答中说明使用了哪条记忆："
            )
            lines += [line for line, _ in applied]
        if blocked:
            lines.append(
                "下列记忆与当前指标契约冲突，未采用；以契约为准，并提示用户复核："
            )
            lines += [f"{line} 冲突：{'；'.join(r)}" for line, r in blocked]
        return "\n".join(lines)


def default_memory() -> Optional[MetricMemory]:
    """Memory enabled by DATATRAIL_METRIC_MEMORY_DB; disabled when unset."""
    path = os.environ.get("DATATRAIL_METRIC_MEMORY_DB", "").strip()
    return MetricMemory(Path(path)) if path else None
