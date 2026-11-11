"""Shared DataTrail execution review for SQL and native chart tools."""

import json
import os

from dbgpt_app.commerce_grain import grain_review
from dbgpt_app.commerce_time import review_time


async def review_commerce_query(state, sql, dialect):
    temporal = await review_time(state, sql, dialect)
    if temporal:
        review, field = temporal, "time_review"
    elif os.environ.get("DATATRAIL_GRAIN_GUARD") == "1":
        try:
            review = grain_review(sql, dialect)
        except Exception:
            review = {
                "code": "COMMERCE_SQL_REVIEW_UNAVAILABLE",
                "reason": "粒度检查未完成，未执行该查询。请简化 SQL 后重试。",
            }
        field = "grain_review"
    else:
        return None
    if not review:
        return None
    return {
        "success": False,
        "error_code": review["code"],
        field: review,
        "chunks": [
            {"output_type": "text", "content": json.dumps(review, ensure_ascii=False)}
        ],
    }
