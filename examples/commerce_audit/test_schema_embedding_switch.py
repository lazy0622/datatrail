import pytest

from dbgpt_serve.datasource.service.db_summary_client import DBSummaryClient


@pytest.mark.parametrize("skip,expected_calls", [("1", 0), ("0", 1)])
def test_sql_only_switch_skips_external_indexing_and_preserves_default(
    monkeypatch, skip, expected_calls
):
    monkeypatch.setenv("DBGPT_SKIP_SCHEMA_EMBEDDING", skip)
    # Avoid application initialization: exercise only the indexing entry point.
    client = object.__new__(DBSummaryClient)
    calls = []
    monkeypatch.setattr(
        client, "create_summary_client", lambda *args: calls.append(args)
    )
    monkeypatch.setattr(client, "init_db_profile", lambda *args: None)
    client.db_summary_embedding("olist_demo", "sqlite")
    assert len(calls) == expected_calls
