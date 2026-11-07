"""Turn-local reuse for successful queries on the static Olist data source."""

import asyncio
import json

import sqlglot
from sqlglot import exp

# Unknown/volatile functions and clock-dependent date functions are not reused.
_DETERMINISTIC_FUNCTIONS = {
    "ABS",
    "AND",
    "AVG",
    "CASE",
    "CAST",
    "COALESCE",
    "COUNT",
    "IF",
    "LENGTH",
    "LOWER",
    "MAX",
    "MIN",
    "NULLIF",
    "OR",
    "ROUND",
    "SUBSTRING",
    "SUM",
    "TRIM",
    "UPPER",
}


def reusable_sql(sql: str, dialect: str) -> bool:
    if dialect != "sqlite":
        return False
    try:
        statements = sqlglot.parse(sql, read="sqlite")
        if len(statements) != 1 or not isinstance(statements[0], exp.Select):
            return False
        return all(
            function.sql_name() in _DETERMINISTIC_FUNCTIONS
            for function in statements[0].find_all(exp.Func)
        )
    except Exception:
        return False


class TurnQueryReuse:
    """Keep one successful result per exact SQL, with a lock per query.

    Construct a new instance for each agent turn. Callers must opt in only for
    the static demo source; this is not a cache for changing business data.
    """

    def __init__(self):
        self._results = {}
        self._locks = {}

    async def execute(self, sql: str, execute):
        key = sql.strip()
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self._results:
                payload = json.loads(self._results[key])
                payload["result_reused"] = True
                payload["reuse_scope"] = "current_turn_static_olist"
                payload["chunks"].append(
                    {
                        "output_type": "text",
                        "content": (
                            "相同 SQL 已在本轮执行成功，复用先前结果及证据编号，"
                            "未再次查询数据库。请利用已有结果继续分析或结束回答。"
                        ),
                    }
                )
                return json.dumps(payload, ensure_ascii=False)
            result = await execute(sql)
            payload = json.loads(result)
            if payload.get("success") is not False and not payload.get("error_code"):
                self._results[key] = result
            return result
