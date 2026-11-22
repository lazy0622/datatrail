"""Cross-session metric memory: confirm before apply, revoke, conflicts, isolation."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dbgpt_app.commerce_contract import load_contract
from dbgpt_app.commerce_memory import MetricDefinition, MetricMemory, MetricMemoryError
from dbgpt_app.openapi.api_v1.commerce_api import router
from dbgpt_app.openapi.api_v1.tools.metric_memory import make_propose_metric_definition

DB = "olist_ecommerce_demo"
GMV = MetricDefinition(
    metric="GMV",
    expression="SUM(price + freight_value)",
    time_field="order_delivered_customer_date",
    filters=["order_status = 'delivered'"],
    currency="BRL",
)


@pytest.fixture
def contract():
    return load_contract("olist")


def test_proposal_applies_only_after_confirmation_and_survives_a_new_session(
    tmp_path, contract
):
    path = tmp_path / "memory.db"
    first = MetricMemory(path)
    record = first.propose("alice", DB, GMV, "conv-1", "GMV 要含运费，按送达时间")
    assert record["status"] == "proposed"
    assert first.prompt("alice", DB, contract) == ""
    first.confirm(record["id"], "alice", contract)

    second_session = MetricMemory(path)  # new process, nothing carried in memory
    prompt = second_session.prompt("alice", DB, contract)
    assert "GMV = SUM(price + freight_value)" in prompt
    assert "order_delivered_customer_date" in prompt
    assert f"记忆 #{record['id']}" in prompt and "conv-1" in prompt


def test_revoked_definition_is_no_longer_applied_but_kept_for_provenance(
    tmp_path, contract
):
    memory = MetricMemory(tmp_path / "memory.db")
    record = memory.propose("alice", DB, GMV, "conv-1", "q")
    memory.confirm(record["id"], "alice", contract)
    memory.revoke(record["id"], "alice")
    assert memory.prompt("alice", DB, contract) == ""
    assert memory.get(record["id"], "alice")["status"] == "revoked"
    with pytest.raises(MetricMemoryError):
        memory.revoke(record["id"], "alice")


@pytest.mark.parametrize(
    "change,reason",
    [({"currency": "USD"}, "币种"), ({"time_field": "shipped_at"}, "时间字段")],
)
def test_definition_conflicting_with_contract_is_refused(
    tmp_path, contract, change, reason
):
    memory = MetricMemory(tmp_path / "memory.db")
    record = memory.propose("alice", DB, GMV.model_copy(update=change), "conv-1", "q")
    with pytest.raises(MetricMemoryError, match=reason):
        memory.confirm(record["id"], "alice", contract)
    assert memory.get(record["id"], "alice")["status"] == "proposed"


def test_contract_change_after_confirmation_blocks_the_memory_and_says_so(
    tmp_path, contract
):
    memory = MetricMemory(tmp_path / "memory.db")
    record = memory.propose("alice", DB, GMV, "conv-1", "q")
    memory.confirm(record["id"], "alice", contract)
    stricter = contract.model_copy(update={"currency": "USD"})
    prompt = memory.prompt("alice", DB, stricter)
    assert "未采用" in prompt and "以契约为准" in prompt
    assert "按此口径生成 SQL" not in prompt


def test_memory_is_isolated_by_owner_and_database(tmp_path, contract):
    memory = MetricMemory(tmp_path / "memory.db")
    record = memory.propose("alice", DB, GMV, "conv-1", "q")
    with pytest.raises(MetricMemoryError):
        memory.confirm(record["id"], "bob", contract)
    memory.confirm(record["id"], "alice", contract)
    assert memory.prompt("bob", DB, contract) == ""
    assert memory.prompt("alice", "chinook", contract) == ""
    with pytest.raises(MetricMemoryError):
        memory.revoke(record["id"], "bob")
    with pytest.raises(MetricMemoryError, match="匿名"):
        memory.propose(" ", DB, GMV, "conv-1", "q")


def test_new_confirmation_supersedes_the_old_definition(tmp_path, contract):
    memory = MetricMemory(tmp_path / "memory.db")
    old = memory.propose("alice", DB, GMV, "conv-1", "q")
    memory.confirm(old["id"], "alice", contract)
    narrower = GMV.model_copy(update={"expression": "SUM(price)"})
    new = memory.propose("alice", DB, narrower, "conv-2", "q")
    memory.confirm(new["id"], "alice", contract)
    prompt = memory.prompt("alice", DB, contract)
    assert "GMV = SUM(price)" in prompt and "freight_value)" not in prompt
    assert memory.get(old["id"], "alice")["status"] == "superseded"


def test_concurrent_confirmations_apply_once(tmp_path, contract):
    memory = MetricMemory(tmp_path / "memory.db")
    record = memory.propose("alice", DB, GMV, "conv-1", "q")

    def attempt(_):
        try:
            MetricMemory(tmp_path / "memory.db").confirm(
                record["id"], "alice", contract
            )
            return True
        except MetricMemoryError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(16))) == 1
    assert len(memory.records("alice", DB, "confirmed")) == 1


def test_model_tool_only_proposes(tmp_path, contract):
    memory = MetricMemory(tmp_path / "memory.db")
    propose = make_propose_metric_definition(
        memory, "alice", DB, "conv-1", "GMV 含运费"
    )
    reply = json.loads(
        asyncio.run(
            propose(
                metric="GMV",
                expression="SUM(price + freight_value)",
                filters="a = 1; b = 2",
            )
        )
    )
    assert reply["success"] and reply["status"] == "proposed"
    stored = memory.get(reply["memory_id"], "alice")
    assert stored["definition"]["filters"] == ["a = 1", "b = 2"]
    assert memory.prompt("alice", DB, contract) == ""


def test_http_confirm_list_revoke_use_the_caller_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("DATATRAIL_METRIC_MEMORY_DB", str(tmp_path / "memory.db"))
    memory = MetricMemory(tmp_path / "memory.db")
    record = memory.propose("alice", DB, GMV, "conv-1", "q")
    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)
    base = f"/api/v1/datatrail/metric-memory/{record['id']}"
    assert client.post(base + "/confirm", headers={"user-id": "bob"}).status_code == 409
    confirmed = client.post(base + "/confirm", headers={"user-id": "alice"})
    assert confirmed.status_code == 200 and confirmed.json()["status"] == "confirmed"
    listed = client.get(
        "/api/v1/datatrail/metric-memory",
        params={"database": DB},
        headers={"user-id": "alice"},
    )
    assert [r["id"] for r in listed.json()] == [record["id"]]
    assert (
        client.post(base + "/revoke", headers={"user-id": "alice"}).json()["status"]
        == "revoked"
    )
    monkeypatch.delenv("DATATRAIL_METRIC_MEMORY_DB")
    assert (
        client.post(base + "/revoke", headers={"user-id": "alice"}).status_code == 404
    )
