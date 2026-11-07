"""Start the upstream DB-GPT server with DataTrail's local configuration."""

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def main():
    os.chdir(ROOT)
    load_dotenv(ROOT / ".env.local", override=False)
    os.environ.setdefault("DATATRAIL_GRAIN_GUARD", "1")
    os.environ.setdefault("DATATRAIL_QUERY_REUSE", "1")
    if not os.environ.get("EMBEDDING_API_KEY"):
        os.environ.setdefault("DBGPT_SKIP_SCHEMA_EMBEDDING", "1")
    os.environ.setdefault(
        "DATATRAIL_DATABASE",
        str(ROOT / "examples/dashboard/olist/data/generated/olist.db"),
    )
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("请先填写 .env.local 中的 OPENAI_API_KEY。")
    key_path = ROOT / "pilot/meta_data/datatrail.key"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if not key_path.exists():
        key_path.write_text(secrets.token_hex(32), encoding="utf-8")
    os.environ.setdefault("DATATRAIL_ENCRYPT_KEY", key_path.read_text().strip())
    os.environ.setdefault("ENCRYPT_KEY", os.environ["DATATRAIL_ENCRYPT_KEY"])
    from dbgpt_app.dbgpt_server import run_webserver

    run_webserver(str(ROOT / "configs/datatrail.example.toml"))


if __name__ == "__main__":
    main()
