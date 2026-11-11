import asyncio
import json

import pytest

from dbgpt_app.commerce_time import review_time, time_contract
from dbgpt_app.openapi.api_v1.tools.question import make_question
from dbgpt_app.openapi.api_v1.tools.question_manager import QuestionManager
from dbgpt_app.openapi.api_v1.tools.sql_query import make_sql_query


class Source:
    dialect = "sqlite"

    def __init__(self):
        self.calls = 0

    def run(self, sql):
        self.calls += 1
        return [["orders"], [3]]


@pytest.mark.asyncio
async def test_unanswered_time_question_prevents_database_execution():
    source = Source()

    async def dismissed(**kwargs):
        return json.dumps({"chunks": [{"content": "dismissed"}]})

    state = {
        "_commerce_time_contract": {"basis": None, "requires_confirmation": True},
        "_commerce_time_question": dismissed,
    }
    query = make_sql_query(state, source)
    result = json.loads(await query("SELECT COUNT(*) FROM olist_orders"))
    assert source.calls == 0
    assert result["error_code"] == "COMMERCE_TIME_BASIS_REQUIRED"


@pytest.mark.asyncio
async def test_confirmed_delivery_time_rejects_purchase_filter_then_allows_rewrite():
    source = Source()
    questions = []

    async def answered(**kwargs):
        questions.append(kwargs)
        return json.dumps({"answers": [["按送达时间"]]})

    state = {
        "_commerce_time_contract": {"basis": None, "requires_confirmation": True},
        "_commerce_time_question": answered,
    }
    query = make_sql_query(state, source)
    rejected = json.loads(
        await query(
            "SELECT COUNT(*) FROM olist_orders "
            "WHERE order_purchase_timestamp >= '2018-07-01'"
        )
    )
    assert rejected["error_code"] == "COMMERCE_TIME_BASIS_MISMATCH"
    assert source.calls == 0
    accepted = json.loads(
        await query(
            "SELECT COUNT(*) FROM olist_orders "
            "WHERE order_delivered_customer_date >= '2018-07-01'"
        )
    )
    assert not accepted.get("error_code")
    assert source.calls == 1 and len(questions) == 1


@pytest.mark.parametrize(
    "text,basis,confirm",
    [
        ("2018年7月已交付订单数", None, True),
        ("按下单时间统计2018年7月已交付订单", "purchase", False),
        ("统计2018年7月送达的订单", "delivery", False),
        ("按默认口径统计2018年7月已交付订单", "purchase", False),
        ("对比下单与送达时间的月度分布", "multiple", False),
        ("总共有多少订单", None, False),
        ("2018年7月不要按送达时间，按下单时间", "purchase", False),
    ],
)
def test_explicit_time_markers_do_not_equate_delivered_status_with_delivery_date(
    text, basis, confirm
):
    contract = time_contract(text)
    assert contract["basis"] == basis
    assert contract["requires_confirmation"] is confirm


@pytest.mark.asyncio
async def test_parallel_queries_share_one_unanswered_question():
    source = Source()
    asked = []

    async def dismissed(**kwargs):
        asked.append(kwargs)
        await asyncio.sleep(0.01)
        return "{}"

    state = {
        "_commerce_time_contract": time_contract("2018年7月已交付订单数"),
        "_commerce_time_question": dismissed,
    }
    query = make_sql_query(state, source)
    results = await asyncio.gather(
        *(query("SELECT COUNT(*) FROM olist_orders") for _ in range(2))
    )
    assert len(asked) == 1 and source.calls == 0
    assert all(
        json.loads(item)["error_code"] == "COMMERCE_TIME_BASIS_REQUIRED"
        for item in results
    )


@pytest.mark.asyncio
async def test_real_question_reply_binds_choice_and_dismissal_does_not_default(
    monkeypatch,
):
    manager = QuestionManager()
    monkeypatch.setattr(
        "dbgpt_app.openapi.api_v1.tools.question.question_manager", manager
    )
    for dismissed in (True, False):
        state = {
            "conv_id": "time-contract",
            "_commerce_time_contract": time_contract("2018年7月已交付订单数"),
        }

        async def capture(kind, payload):
            if kind == "question.asked":
                if dismissed:
                    manager.reject(payload["request_id"])
                else:
                    manager.reply(payload["request_id"], [["按下单时间"]])

        state["_commerce_time_question"] = make_question(state, capture)
        source = Source()
        result = json.loads(
            await make_sql_query(state, source)(
                "SELECT COUNT(*) FROM olist_orders "
                "WHERE order_purchase_timestamp >= '2018-07-01'"
            )
        )
        assert source.calls == (0 if dismissed else 1)
        if dismissed:
            assert result["error_code"] == "COMMERCE_TIME_BASIS_REQUIRED"
        else:
            assert state["_commerce_time_contract"]["basis"] == "purchase"
        assert manager.list_pending() == []


@pytest.mark.asyncio
async def test_model_initiated_question_does_not_require_a_second_confirmation(
    monkeypatch,
):
    manager = QuestionManager()
    monkeypatch.setattr(
        "dbgpt_app.openapi.api_v1.tools.question.question_manager", manager
    )
    state = {
        "conv_id": "already-answered",
        "_commerce_time_contract": time_contract("2018年7月已交付订单数"),
    }
    events = []

    async def capture(kind, payload):
        events.append(kind)
        if kind == "question.asked":
            manager.reply(payload["request_id"], [["按送达时间"]])

    ask = make_question(state, capture)
    state["_commerce_time_question"] = ask
    await ask(questions=[{"question": "按哪种时间归属？"}])
    source = Source()
    await make_sql_query(state, source)(
        "SELECT COUNT(*) FROM olist_orders "
        "WHERE order_delivered_customer_date >= '2018-07-01'"
    )
    assert source.calls == 1 and events.count("question.asked") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql,blocked",
    [
        (
            "SELECT COUNT(*) FROM olist_orders "
            "WHERE substr(order_delivered_customer_date,1,7)='2018-07'",
            True,
        ),
        (
            "SELECT COUNT(*) FROM olist_orders "
            "WHERE order_delivered_customer_date "
            "BETWEEN '2018-07-01' AND '2018-08-01'",
            True,
        ),
        (
            "SELECT julianday(order_delivered_customer_date)"
            "-julianday(order_purchase_timestamp) FROM olist_orders "
            "WHERE order_purchase_timestamp>='2018-07-01'",
            False,
        ),
        (
            "SELECT COUNT(*) FROM olist_orders "
            "WHERE order_purchase_timestamp>='2018-07-01' "
            "AND order_delivered_customer_date IS NOT NULL",
            False,
        ),
    ],
)
async def test_filter_review_does_not_block_delivery_duration_or_null_checks(
    sql, blocked
):
    state = {"_commerce_time_contract": time_contract("按下单时间统计2018年7月订单")}
    assert bool(await review_time(state, sql)) is blocked


@pytest.mark.asyncio
async def test_chart_tool_cannot_bypass_the_time_contract():
    from dbgpt_app.openapi.api_v1.tools.chart_query import make_chart_query

    state = {"_commerce_time_contract": time_contract("按下单时间统计2018年7月订单")}
    source = Source()
    result = json.loads(
        await make_chart_query(state, source)(
            "SELECT COUNT(*) FROM olist_orders "
            "WHERE order_delivered_customer_date >= '2018-07-01'"
        )
    )
    assert result["error_code"] == "COMMERCE_TIME_BASIS_MISMATCH"
    assert source.calls == 0


@pytest.mark.asyncio
async def test_model_initiated_time_question_dismissal_does_not_ask_twice(monkeypatch):
    manager = QuestionManager()
    monkeypatch.setattr(
        "dbgpt_app.openapi.api_v1.tools.question.question_manager", manager
    )
    state = {
        "conv_id": "dismissed",
        "_commerce_time_contract": time_contract("2018年7月已交付订单数"),
    }
    events = []

    async def capture(kind, payload):
        events.append(kind)
        if kind == "question.asked":
            manager.reject(payload["request_id"])

    ask = make_question(state, capture)
    state["_commerce_time_question"] = ask
    await ask(questions=[{"question": "按下单还是送达时间？"}])
    repeated = json.loads(await ask(questions=[{"question": "按下单还是送达时间？"}]))
    assert repeated["error_code"] == "COMMERCE_TIME_BASIS_REQUIRED"
    source = Source()
    result = json.loads(
        await make_sql_query(state, source)("SELECT COUNT(*) FROM olist_orders")
    )
    assert result["error_code"] == "COMMERCE_TIME_BASIS_REQUIRED"
    assert source.calls == 0 and events.count("question.asked") == 1
