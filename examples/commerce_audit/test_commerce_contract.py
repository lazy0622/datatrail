import json

import pytest

from dbgpt_app.commerce_contract import load_contract, resolve_contract_for_database
from dbgpt_app.commerce_grain import grain_review
from dbgpt_app.openapi.api_v1.tools.sql_query import make_sql_query


class _RecordingConnector:
    dialect = "sqlite"

    def __init__(self):
        self.calls = 0

    def run(self, sql):
        self.calls += 1
        return [[("value",)], [42]]


def test_builtin_contracts_load_and_resolve():
    olist = resolve_contract_for_database("olist_ecommerce_demo")
    chinook = resolve_contract_for_database("Chinook")
    assert olist is not None and olist.dataset_name == "olist_ecommerce_demo"
    assert chinook is not None and chinook.dataset_name == "chinook"
    assert resolve_contract_for_database("unknown_db") is None


def test_chinook_contract_flags_parent_child_and_m2m_fanout_but_allows_exists():
    chinook = load_contract("chinook")
    parent_child = grain_review(
        "SELECT SUM(i.Total) FROM Invoice i "
        "JOIN InvoiceLine il ON il.InvoiceId=i.InvoiceId",
        contract=chinook,
    )
    assert parent_child is not None
    assert parent_child["rule_id"] == "chinook_invoice_invoiceline_fanout"

    m2m = grain_review(
        "SELECT SUM(il.UnitPrice * il.Quantity) FROM InvoiceLine il "
        "JOIN Track t ON t.TrackId=il.TrackId "
        "JOIN PlaylistTrack pt ON pt.TrackId=t.TrackId",
        contract=chinook,
    )
    assert m2m is not None
    assert m2m["rule_id"] == "chinook_invoiceline_playlisttrack_fanout"

    safe_exists = grain_review(
        "SELECT SUM(il.UnitPrice * il.Quantity) FROM InvoiceLine il "
        "JOIN Track t ON t.TrackId=il.TrackId "
        "WHERE EXISTS (SELECT 1 FROM PlaylistTrack pt WHERE pt.TrackId=t.TrackId)",
        contract=chinook,
    )
    assert safe_exists is None

    safe_header = grain_review("SELECT SUM(Total) FROM Invoice", contract=chinook)
    assert safe_header is None


def test_custom_external_contract_works_without_code_changes(tmp_path, monkeypatch):
    contract_file = tmp_path / "clinic.json"
    contract_file.write_text(
        json.dumps(
            {
                "dataset_name": "clinic_billing",
                "currency": "CNY",
                "default_filters": ["visit_status = 'closed'"],
                "grain_rules": [
                    {
                        "id": "visit_prescription_fanout",
                        "rule_type": "parent_child_fanout",
                        "left_tables": ["visits"],
                        "right_tables": ["prescriptions"],
                        "left_amount_columns": ["registration_fee"],
                        "right_amount_columns": [],
                        "allow_unqualified": False,
                        "guidance": (
                            "visits 与 prescriptions 关联后勿直接 "
                            "SUM(v.registration_fee)。"
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DATATRAIL_METRIC_CONTRACT", str(contract_file))
    resolved = resolve_contract_for_database("anything")
    assert resolved is not None and resolved.dataset_name == "clinic_billing"
    hit = grain_review(
        "SELECT SUM(v.registration_fee) FROM visits v "
        "JOIN prescriptions p ON p.visit_id=v.id",
        contract=resolved,
    )
    assert hit is not None and hit["rule_id"] == "visit_prescription_fanout"


def test_olist_contract_matches_default_grain_review():
    olist = load_contract("olist")
    risky = (
        "SELECT SUM(i.price+i.freight_value) FROM olist_order_items i "
        "JOIN olist_order_payments p ON p.order_id=i.order_id"
    )
    safe = (
        "SELECT SUM(i.price) FROM olist_order_items i "
        "WHERE EXISTS "
        "(SELECT 1 FROM olist_order_payments p WHERE p.order_id=i.order_id)"
    )
    assert grain_review(risky, contract=olist)["code"] == grain_review(risky)["code"]
    assert grain_review(safe, contract=olist) is None and grain_review(safe) is None


@pytest.mark.asyncio
async def test_sql_query_uses_state_metric_contract_before_db_execution(monkeypatch):
    monkeypatch.setenv("DATATRAIL_GRAIN_GUARD", "1")
    connector = _RecordingConnector()
    state = {"_commerce_metric_contract": load_contract("chinook")}
    query = make_sql_query(state, connector)

    blocked = json.loads(
        await query(
            "SELECT SUM(i.Total) FROM Invoice i "
            "JOIN InvoiceLine il ON il.InvoiceId=i.InvoiceId"
        )
    )
    assert connector.calls == 0
    assert blocked["error_code"] == "COMMERCE_GRAIN_REVIEW_REQUIRED"
    assert blocked["grain_review"]["rule_id"] == "chinook_invoice_invoiceline_fanout"

    allowed = json.loads(await query("SELECT SUM(Total) FROM Invoice"))
    assert connector.calls == 1
    assert not allowed.get("error_code")
