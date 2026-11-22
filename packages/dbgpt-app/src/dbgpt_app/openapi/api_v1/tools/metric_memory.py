"""ReAct tool that lets the model propose (never apply) a metric definition."""

import json

from pydantic import ValidationError

from dbgpt.agent.resource.tool.base import tool
from dbgpt_app.commerce_memory import MetricDefinition, MetricMemory, MetricMemoryError


def _reply(success: bool, content: str, **extra) -> str:
    return json.dumps(
        {
            "success": success,
            **extra,
            "chunks": [{"output_type": "text", "content": content}],
        },
        ensure_ascii=False,
    )


def make_propose_metric_definition(
    memory: MetricMemory, owner: str, database: str, conv_uid: str, question: str
):
    @tool(
        description=(
            "当用户在本轮明确说明某个指标的计算口径（如 GMV 是否含运费、"
            "按哪个时间字段、排除哪些状态）时，记录一条待确认的口径。"
            "只记录用户说过的口径，不要自行推测。"
            "记录后不会自动生效，需要用户在口径管理中确认后，之后的会话才会使用。"
            '参数: {"metric":"GMV","expression":"SUM(price + freight_value)",'
            '"time_field":"order_delivered_customer_date",'
            '"filters":"order_status = \'delivered\'","currency":"BRL",'
            '"note":"用户说明"}。'
            "filters 多个条件用英文分号分隔。"
        )
    )
    async def propose_metric_definition(
        metric: str,
        expression: str,
        time_field: str = "",
        filters: str = "",
        currency: str = "",
        note: str = "",
    ) -> str:
        try:
            definition = MetricDefinition(
                metric=metric,
                expression=expression,
                time_field=time_field or None,
                filters=[f.strip() for f in filters.split(";") if f.strip()],
                currency=currency or None,
                note=note,
            )
            record = memory.propose(owner, database, definition, conv_uid, question)
        except (ValidationError, MetricMemoryError) as exc:
            return _reply(
                False, f"口径未记录：{exc}", error_code="METRIC_MEMORY_REJECTED"
            )
        return _reply(
            True,
            f"已记录待确认口径 #{record['id']}（{definition.metric}）。"
            "请告知用户：确认后，之后的会话才会按此口径计算。",
            memory_id=record["id"],
            status=record["status"],
        )

    return propose_metric_definition
