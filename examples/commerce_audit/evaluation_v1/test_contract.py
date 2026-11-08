import pytest
from sqlalchemy import create_engine, text

from examples.commerce_audit.evaluation_v1.run_comparison import parse_final, rows_match


@pytest.mark.parametrize(
    "actual,expected,correct",
    [
        ([[10, None]], [[10.0, None]], True),
        ([[10, 0]], [[10, None]], False),
        ([[1000.01]], [[1000]], False),
        ([["1,000"]], [[1000]], False),
        ([[1], [2]], [[2], [1]], False),
        (None, [[1]], False),
    ],
)
def test_numeric_grader_preserves_nulls_precision_and_order(actual, expected, correct):
    assert rows_match(actual, expected) is correct


def test_final_json_accepts_code_fence_but_not_prose():
    assert parse_final('```json\n{"status":"ok","rows":[[1]]}\n```')["rows"] == [[1]]
    with pytest.raises(ValueError):
        parse_final("amount is 1")


def test_grader_accepts_database_rows_without_stringifying_them():
    engine = create_engine("sqlite://")
    try:
        with engine.connect() as connection:
            rows = connection.execute(text("SELECT 1.25, NULL")).fetchall()
        assert rows_match(rows, [[1.25, None]])
        assert [list(row) for row in rows] == [[1.25, None]]
    finally:
        engine.dispose()
