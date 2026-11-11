"""Bounded Olist time-basis confirmation and date-filter review.

This recognizes common explicit markers, not arbitrary natural-language intent.
Only the current turn's state is used; unanswered questions never imply consent.
"""

import asyncio
import json
import re

import sqlglot
from sqlglot import exp

_SCOPE = re.compile(r"\d{4}[-年]|\d{1,2}月|本月|上月|今年|去年|季度|月度|最近", re.I)
_PURCHASE = re.compile(
    r"下单|购买时间|订单创建|order_purchase_timestamp|purchase|order creation", re.I
)
_DELIVERY = re.compile(
    r"送达|签收|实际交付|收货时间|order_delivered_customer_date|delivery date", re.I
)
FIELDS = {
    "purchase": "order_purchase_timestamp",
    "delivery": "order_delivered_customer_date",
}
_TIME_FIELDS = set(FIELDS.values()) | {
    "order_approved_at",
    "order_delivered_carrier_date",
    "order_estimated_delivery_date",
}


def explicit_basis(text):
    text = str(text or "")
    text = re.sub(r"(?:不要按|不按|不使用)(?:下单|送达)(?:日期|时间)?", "", text)
    purchase, delivery = bool(_PURCHASE.search(text)), bool(_DELIVERY.search(text))
    if purchase and delivery:
        return (
            "multiple"
            if re.search(r"分别|对比|同时|比较|并且|且|both|compare", text, re.I)
            else None
        )
    if purchase:
        return "purchase"
    if delivery:
        return "delivery"
    return None


def time_contract(user_input):
    basis = explicit_basis(user_input)
    if basis is None and re.search(r"按默认|默认口径", user_input):
        basis = "purchase"
    return {
        "basis": basis,
        "requires_confirmation": bool(_SCOPE.search(user_input)) and basis is None,
        "asked": False,
    }


def apply_time_answers(state, answers):
    contract = state.get("_commerce_time_contract")
    if not contract or contract.get("basis") is not None:
        return
    basis = explicit_basis(" ".join(value for row in answers for value in row))
    if basis in FIELDS:
        contract["basis"] = basis


def note_time_question(state, questions):
    contract = state.get("_commerce_time_contract")
    text = json.dumps(questions, ensure_ascii=False)
    if contract and (_PURCHASE.search(text) or _DELIVERY.search(text)):
        if contract.get("asked") and contract.get("basis") is None:
            return False
        contract["asked"] = True
    return True


async def review_time(state, sql, dialect="sqlite"):
    contract = state.get("_commerce_time_contract")
    if contract is None:
        return None
    if contract.get("requires_confirmation") and contract.get("basis") is None:
        lock = state.setdefault("_commerce_time_lock", asyncio.Lock())
        async with lock:
            if contract.get("basis") is None and not contract.get("asked"):
                ask = state.get("_commerce_time_question")
                if ask is not None:
                    answer = json.loads(
                        await ask(
                            questions=[
                                {
                                    "question": (
                                        "这次统计的月份/日期范围按下单时间，"
                                        "还是实际送达时间归属？"
                                    ),
                                    "header": "统计时间口径",
                                    "options": [
                                        {"label": "按下单时间"},
                                        {"label": "按送达时间"},
                                    ],
                                    "multiple": False,
                                    "custom": True,
                                }
                            ]
                        )
                    )
                    apply_time_answers(state, answer.get("answers") or [])
                contract["asked"] = True
        if contract.get("basis") is None:
            return {
                "code": "COMMERCE_TIME_BASIS_REQUIRED",
                "reason": "统计时间口径尚未确认，本轮未执行该 SQL。",
                "next_action": (
                    "请先确认按下单或送达时间；取消或超时不代表同意默认口径。"
                ),
            }
    basis = contract.get("basis")
    if basis not in FIELDS:
        return None
    expected = FIELDS[basis]
    tree = sqlglot.parse_one(sql, read=dialect)
    for column in tree.find_all(exp.Column):
        if column.name.lower() not in _TIME_FIELDS - {expected}:
            continue
        if column.find_ancestor(exp.Where) is None:
            continue
        predicate = column.find_ancestor(
            exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.In, exp.Like
        )
        if predicate is not None and any(
            literal.is_string and re.match(r"\d{4}(?:-|$)", literal.this)
            for literal in predicate.find_all(exp.Literal)
        ):
            return {
                "code": "COMMERCE_TIME_BASIS_MISMATCH",
                "reason": "查询的日期筛选与用户确认的时间口径不一致。",
                "confirmed_field": expected,
                "observed_field": column.name,
                "next_action": (
                    f"将统计日期筛选改为 {expected}，保留用户的状态和日期范围。"
                ),
            }
    return None
