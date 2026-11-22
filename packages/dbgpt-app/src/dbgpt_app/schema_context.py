"""Compact schema context for text-to-SQL: column notes, sample values, matched values.

A bare CREATE TABLE statement tells the model names and types only. Many wrong queries
come from guessing what a column means or how its values are written ('Student_Club'
vs 'Club', '2019-09-04' vs '9/4/2019'). This module adds three things, each bounded in
size:

1. Column notes: a human description of the column and its values, when available.
2. Sample values: the most frequent values of each column, which show formats.
3. Matched values: database values that literally match words or phrases in the
   question, with the column they live in.

It reads through a ``run(sql) -> rows`` callable, so it works with sqlite3 or any
connector that can run a SELECT.
"""

import csv
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

Rows = list[tuple]
Run = Callable[[str], Rows]

STOPWORDS = set(
    "a an and are as at be by did do does for from has have how in is it its many "
    "much of on or the their there this to was were what when where which who whom "
    "whose why with list name names give show find state number count total average "
    "percentage ratio please all each any among between than more less most least "
    "top highest lowest".split()
)


@dataclass
class ColumnNote:
    description: str = ""
    values: str = ""


@dataclass
class ValueIndex:
    """Distinct short text values of low-cardinality columns, by normalized text."""

    by_text: dict[str, set[tuple[str, str, str]]] = field(default_factory=dict)

    def add(self, table: str, column: str, value: str) -> None:
        self.by_text.setdefault(normalize(value), set()).add((table, column, value))

    def match(self, text: str, limit: int = 20) -> list[tuple[str, str, str]]:
        """Values equal (case-insensitively) to a 1-4 word phrase of the text."""
        found: list[tuple[str, str, str]] = []
        for phrase in phrases(text):
            for hit in sorted(self.by_text.get(phrase, ())):
                if hit not in found:
                    found.append(hit)
        return found[:limit]


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def phrases(text: str) -> list[str]:
    """Quoted strings first, then 4- to 1-word phrases that are not all stopwords."""
    quoted = [
        normalize(q) for q in re.findall(r"['\"“”‘’]([^'\"“”‘’]{2,60})['\"“”‘’]", text)
    ]
    words = re.findall(r"[A-Za-z0-9_][A-Za-z0-9_.'&-]*", text)
    out = list(quoted)
    for size in (4, 3, 2, 1):
        for start in range(len(words) - size + 1):
            chunk = words[start : start + size]
            if all(w.lower() in STOPWORDS for w in chunk):
                continue
            if size == 1 and (len(chunk[0]) < 3 or chunk[0].isdigit()):
                continue
            phrase = normalize(" ".join(chunk).strip(".'"))
            if phrase not in out:
                out.append(phrase)
    return out


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def load_bird_notes(directory: Path) -> dict[tuple[str, str], ColumnNote]:
    """Read BIRD database_description/*.csv files (one per table)."""
    notes: dict[tuple[str, str], ColumnNote] = {}
    for path in sorted(directory.glob("*.csv")):
        raw = path.read_bytes()
        for encoding in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        for row in csv.DictReader(text.splitlines()):
            column = (row.get("original_column_name") or "").strip()
            if not column:
                continue
            notes[(path.stem.lower(), column.lower())] = ColumnNote(
                description=" ".join((row.get("column_description") or "").split()),
                values=" ".join((row.get("value_description") or "").split()),
            )
    return notes


def is_text(kind: str) -> bool:
    return not kind or any(t in kind for t in ("CHAR", "TEXT", "CLOB", "DATE"))


def table_columns(run: Run) -> dict[str, list[tuple[str, str]]]:
    tables = [
        r[0]
        for r in run(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {
        t: [(r[1], (r[2] or "").upper()) for r in run(f"PRAGMA table_info({quote(t)})")]
        for t in tables
    }


def build_value_index(
    run: Run, max_distinct: int = 5000, max_length: int = 80
) -> ValueIndex:
    """Index text columns with few distinct values; skip ids and free text."""
    index = ValueIndex()
    for table, columns in table_columns(run).items():
        for column, kind in columns:
            if not is_text(kind):
                continue
            q = quote(column)
            stats = run(
                f"SELECT count(DISTINCT {q}), max(length({q})) FROM {quote(table)} "
                f"WHERE typeof({q}) = 'text'"
            )
            distinct, longest = stats[0] if stats else (0, 0)
            if not distinct or distinct > max_distinct or (longest or 0) > max_length:
                continue
            for (value,) in run(
                f"SELECT DISTINCT {q} FROM {quote(table)} WHERE typeof({q}) = 'text'"
            ):
                index.add(table, column, value)
    return index


def all_samples(run: Run, limit: int = 3) -> dict[tuple[str, str], list[str]]:
    """Most frequent values of each text column; they show how values are written."""
    samples = {}
    for table, columns in table_columns(run).items():
        for column, kind in columns:
            if not is_text(kind):
                continue
            rows = run(
                f"SELECT {quote(column)}, count(*) c FROM {quote(table)} "
                f"WHERE {quote(column)} IS NOT NULL "
                f"GROUP BY 1 ORDER BY c DESC LIMIT {int(limit)}"
            )
            samples[(table, column)] = [str(r[0])[:40] for r in rows]
    return samples


def build_context(
    run: Run,
    question: str,
    notes: dict[tuple[str, str], ColumnNote] | None = None,
    index: ValueIndex | None = None,
    samples: dict[tuple[str, str], list[str]] | None = None,
) -> str:
    """Text block to append after the DDL. Every part is optional."""
    lines: list[str] = []
    if notes is not None or samples is not None:
        lines.append(
            "Column notes (table.column: meaning | value format | frequent values):"
        )
        for table, columns in table_columns(run).items():
            for column, _ in columns:
                key = (table.lower(), column.lower())
                note = (notes or {}).get(key, ColumnNote())
                parts = [p for p in (note.description, note.values) if p]
                if samples is not None and samples.get((table, column)):
                    parts.append(
                        "e.g. " + ", ".join(repr(v) for v in samples[(table, column)])
                    )
                if parts:
                    lines.append(f"- {table}.{column}: " + " | ".join(parts))
    if index is not None:
        hits = index.match(question)
        if hits:
            lines.append("Database values that match words in the question:")
            lines += [
                f"- {table}.{column} = {value!r}" for table, column, value in hits
            ]
    return "\n".join(lines)
