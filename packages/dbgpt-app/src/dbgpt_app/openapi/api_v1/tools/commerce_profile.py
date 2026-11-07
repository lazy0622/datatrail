"""DataTrail profiling tool, limited to the already-selected SQLite source."""

import asyncio
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from sqlalchemy.engine import make_url

from dbgpt.agent.resource.tool.base import tool
from dbgpt_app.commerce_profile import diagnose


def profile_connector(connector) -> dict:
    if connector is None or connector.dialect != "sqlite":
        raise ValueError("当前诊断只支持已选择的 SQLite Olist 数据源。")
    path = Path(make_url(connector.db_url).database).resolve(strict=True)
    return profile_path(path)


def profile_path(path: Path, timeout_seconds: float = 15) -> dict:
    deadline = time.monotonic() + timeout_seconds
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.set_progress_handler(lambda: time.monotonic() > deadline, 10000)
        connection.execute("BEGIN")
        return diagnose(connection)


def make_commerce_profile(connector):
    @tool(
        description=(
            "诊断当前 Olist SQLite 数据源中商品明细与支付明细的关联放大风险。"
            "无参数。只读，固定 SQL，最多 15 秒。结果范围是已交付且同时有商品、"
            "支付记录的订单；不是用户任意 SQL 的正确性证明。"
        )
    )
    async def commerce_profile() -> str:
        try:
            report = await asyncio.to_thread(profile_connector, connector)
            return json.dumps(
                {
                    "chunks": [
                        {
                            "output_type": "text",
                            "content": json.dumps(report, ensure_ascii=False),
                        }
                    ]
                },
                ensure_ascii=False,
            )
        except (ValueError, OSError, sqlite3.Error) as exc:
            return json.dumps(
                {
                    "success": False,
                    "chunks": [
                        {
                            "output_type": "text",
                            "content": str(exc),
                        }
                    ],
                },
                ensure_ascii=False,
            )

    return commerce_profile
