"""Read-only, data-level order/item/payment join diagnostic (not an LLM eval)."""

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from dbgpt_app.commerce_profile import diagnose


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    path = args.db.resolve(strict=True)
    if args.output and args.output.resolve() == path:
        parser.error("The report must not overwrite the database.")
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        report = diagnose(connection)
    report.update(database=str(path), database_sha256=digest)
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")


if __name__ == "__main__":
    main()
