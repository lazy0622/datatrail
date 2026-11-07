import asyncio
import json
import sqlite3
from threading import Event

import pytest

from dbgpt_app.openapi.api_v1.tools.query_reuse import reusable_sql
from dbgpt_app.openapi.api_v1.tools.sql_query import make_sql_query


class SQLiteSource:
    dialect = "sqlite"

    def __init__(self, path):
        self.path = path
        self.calls = 0
        self.started = None
        self.release = None

    def run(self, sql):
        self.calls += 1
        if self.started is not None:
            self.started.set()
            assert self.release.wait(3), "Test query was not released"
        with sqlite3.connect(self.path) as connection:
            cursor = connection.execute(sql)
            return [[column[0] for column in cursor.description], *cursor.fetchall()]


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE items(price REAL); INSERT INTO items VALUES (10),(10);"
        )
    return SQLiteSource(path)


@pytest.mark.parametrize("reuse,expected_calls", [(False, 2), (True, 1)])
def test_repeat_query_reuses_result_and_keeps_original_artifact(
    source, monkeypatch, reuse, expected_calls
):
    captures = []

    def capture(**kwargs):
        captures.append(kwargs)
        return f"sql-{len(captures)}"

    monkeypatch.setattr(
        "dbgpt_app.openapi.api_v1.tools.sql_query.capture_sql_execution", capture
    )
    query = make_sql_query({}, source, reuse_results=reuse)
    first = json.loads(query("SELECT SUM(price) AS total FROM items"))
    second = json.loads(query("SELECT SUM(price) AS total FROM items"))
    assert source.calls == expected_calls
    assert len(captures) == expected_calls
    assert "20" in second["chunks"][0]["content"]
    if reuse:
        assert first["artifact_id"] == second["artifact_id"] == "sql-1"
        assert second["result_reused"] is True
        assert "result_reused" not in first


def test_different_literals_are_not_collapsed(source):
    query = make_sql_query({}, source, reuse_results=True)
    upper = json.loads(query("SELECT 'A' AS label"))
    lower = json.loads(query("SELECT 'a' AS label"))
    assert "| A |" in upper["chunks"][0]["content"]
    assert "| a |" in lower["chunks"][0]["content"]
    assert source.calls == 2


def test_filtered_business_aggregate_is_reused(source):
    query = make_sql_query({}, source, reuse_results=True)
    sql = (
        "SELECT COUNT(*), ROUND(SUM(price),2) FROM items "
        "WHERE (price >= 10 AND price <= 20) OR price=30"
    )
    query(sql)
    result = json.loads(query(sql))
    assert source.calls == 1
    assert result["result_reused"] is True
    assert "20" in result["chunks"][0]["content"]


def test_new_turn_reads_updated_database(source):
    first_turn = make_sql_query({}, source, reuse_results=True)
    first_turn("SELECT SUM(price) AS total FROM items")
    with sqlite3.connect(source.path) as connection:
        connection.execute("INSERT INTO items VALUES (30)")
    second_turn = make_sql_query({}, source, reuse_results=True)
    result = json.loads(second_turn("SELECT SUM(price) AS total FROM items"))
    assert source.calls == 2
    assert "50" in result["chunks"][0]["content"]


def test_failed_query_is_retried_after_database_repair(source):
    query = make_sql_query({}, source, reuse_results=True)
    failed = json.loads(query("SELECT COUNT(*) FROM repaired"))
    assert failed["success"] is False
    with sqlite3.connect(source.path) as connection:
        connection.execute("CREATE TABLE repaired(value INTEGER)")
    result = json.loads(query("SELECT COUNT(*) FROM repaired"))
    assert source.calls == 2
    assert "error_code" not in result
    assert "result_reused" not in result


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT RANDOM()",
        "SELECT CURRENT_TIMESTAMP",
        "SELECT datetime('now')",
        "SELECT custom_function(price) FROM items",
    ],
)
def test_volatile_and_unknown_functions_execute_again(source, sql):
    assert reusable_sql(sql, "sqlite") is False
    query = make_sql_query({}, source, reuse_results=True)
    query(sql)
    query(sql)
    assert source.calls == 2


@pytest.mark.asyncio
async def test_parallel_duplicate_queries_share_one_execution(source):
    source.started, source.release = Event(), Event()
    query = make_sql_query({}, source, reuse_results=True)
    sql = "SELECT SUM(price) FROM items"
    first = asyncio.create_task(query(sql))
    try:
        assert await asyncio.to_thread(source.started.wait, 2)
        second = asyncio.create_task(query(sql))
        await asyncio.sleep(0)
        source.release.set()
        results = [json.loads(value) for value in await asyncio.gather(first, second)]
        assert source.calls == 1
        assert sum(bool(result.get("result_reused")) for result in results) == 1
    finally:
        source.release.set()


@pytest.mark.asyncio
async def test_cancellation_does_not_cache_or_hold_query_lock(source):
    source.started, source.release = Event(), Event()
    query = make_sql_query({}, source, reuse_results=True)
    sql = "SELECT SUM(price) FROM items"
    task = asyncio.create_task(query(sql))
    try:
        assert await asyncio.to_thread(source.started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        source.release.set()
        result = json.loads(await query(sql))
        assert source.calls == 2
        assert "result_reused" not in result
        assert "20" in result["chunks"][0]["content"]
    finally:
        source.release.set()
