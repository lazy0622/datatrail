import hashlib
import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dbgpt_app.openapi.api_v1.commerce_api import router
from dbgpt_app.openapi.api_v1.tools.commerce_profile import profile_path
from dbgpt_app.openapi.api_v1.tools.sql_query import make_sql_query

BAD_SQL = """SELECT SUM(i.price) FROM order_items i
JOIN order_payments p ON p.order_id=i.order_id"""


class Connector:
    dialect = "sqlite"

    def __init__(self):
        self.calls = 0

    def run(self, sql):
        self.calls += 1
        return [["amount"], [20]]


def test_review_feedback_prevents_execution_then_allows_exists_rewrite(monkeypatch):
    monkeypatch.setenv("DATATRAIL_GRAIN_GUARD", "1")
    connector = Connector()
    query = make_sql_query({}, connector)
    rejected = json.loads(query(BAD_SQL))
    assert connector.calls == 0
    assert rejected["error_code"] == "COMMERCE_GRAIN_REVIEW_REQUIRED"
    corrected = json.loads(
        query(
            "SELECT SUM(i.price) FROM order_items i WHERE EXISTS "
            "(SELECT 1 FROM order_payments p WHERE p.order_id=i.order_id)"
        )
    )
    assert connector.calls == 1
    assert "20" in corrected["chunks"][0]["content"]


def test_baseline_switch_preserves_upstream_execution(monkeypatch):
    monkeypatch.delenv("DATATRAIL_GRAIN_GUARD", raising=False)
    connector = Connector()
    result = json.loads(make_sql_query({}, connector)(BAD_SQL))
    assert connector.calls == 1
    assert "error_code" not in result


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "olist.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE orders(order_id TEXT,order_status TEXT);"
            "CREATE TABLE order_items(order_id TEXT,price REAL);"
            "CREATE TABLE order_payments(order_id TEXT,payment_value REAL);"
            "INSERT INTO orders VALUES ('a','delivered');"
            "INSERT INTO order_items VALUES ('a',10),('a',10);"
            "INSERT INTO order_payments VALUES ('a',5),('a',15);"
        )
    return path


def test_profile_reads_snapshot_without_modifying_database(database):
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    result = profile_path(database)
    assert result["item_amount_before_payment_join"] == 20
    assert result["item_amount_after_naive_payment_join"] == 40
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_api_uses_configured_database_and_handles_missing_config(monkeypatch, database):
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    monkeypatch.delenv("DATATRAIL_DATABASE", raising=False)
    assert client.get("/v1/commerce/profile").status_code == 503
    monkeypatch.setenv("DATATRAIL_DATABASE", str(database))
    response = client.get("/v1/commerce/profile")
    assert response.status_code == 200
    assert response.json()["profile"]["matched_order_count"] == 1


def test_profile_cooperatively_stops_when_deadline_has_expired(database):
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO order_items VALUES (?,?)", [("a", 1)] * 5000
        )
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        profile_path(database, timeout_seconds=-1)
