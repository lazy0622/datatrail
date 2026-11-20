"""Configurable metric, time-basis and grain-guard contracts for SQL databases."""

import json
import os
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

_CONTRACTS_DIR = Path(__file__).resolve().parents[4] / "configs" / "metric_contracts"


class TimeContractSpec(BaseModel):
    """Declarative time-column conventions for one dataset."""

    model_config = ConfigDict(extra="forbid")
    default_field: str = Field(min_length=1)
    fields: dict[str, str] = Field(default_factory=dict)
    other_time_fields: list[str] = Field(default_factory=list)
    requires_confirmation: bool = False


class OneRowFilter(BaseModel):
    """Equality filter that leaves at most one row of `table` per join key."""

    model_config = ConfigDict(extra="forbid")
    table: str = Field(min_length=1)
    column: str = Field(min_length=1)
    value: str | int


class GrainRule(BaseModel):
    """Declarative fan-out join pattern to review before SQL execution."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    rule_type: Literal["sibling_detail_fanout", "parent_child_fanout"]
    left_tables: list[str] = Field(min_length=1)
    right_tables: list[str] = Field(min_length=1)
    left_amount_columns: list[str] = Field(default_factory=list)
    right_amount_columns: list[str] = Field(default_factory=list)
    allow_unqualified: bool = False
    one_row_filters: list[OneRowFilter] = Field(default_factory=list)
    guidance: str = Field(min_length=1)


class MetricContract(BaseModel):
    """Schema-agnostic contract separating dataset rules from Python guard logic."""

    model_config = ConfigDict(extra="forbid")
    dataset_name: str = Field(min_length=1)
    currency: str = Field(default="")
    default_filters: list[str] = Field(default_factory=list)
    time_contract: Optional[TimeContractSpec] = None
    grain_rules: list[GrainRule] = Field(default_factory=list)

    def prompt_summary(self) -> str:
        lines = [f"## {self.dataset_name} 指标与粒度契约"]
        if self.currency:
            lines.append(f"- 默认币种：{self.currency}。")
        if self.default_filters:
            lines.append("- 默认过滤：" + "；".join(self.default_filters) + "。")
        if self.time_contract:
            lines.append(f"- 默认时间字段：{self.time_contract.default_field}。")
        for rule in self.grain_rules:
            lines.append(f"- 粒度约束（{rule.id}）：{rule.guidance}")
        return "\n".join(lines)


def contracts_directory() -> Path:
    return Path(os.environ.get("DATATRAIL_CONTRACTS_DIR", str(_CONTRACTS_DIR)))


def load_contract(name_or_path: str | Path) -> MetricContract:
    candidate = Path(name_or_path)
    if not candidate.is_file():
        normalized = str(name_or_path).strip().lower()
        candidate = contracts_directory() / (
            normalized if normalized.endswith(".json") else f"{normalized}.json"
        )
    data = json.loads(candidate.read_text(encoding="utf-8"))
    return MetricContract.model_validate(data)


def resolve_contract_for_database(
    database_name: Optional[str],
) -> Optional[MetricContract]:
    override = os.environ.get("DATATRAIL_METRIC_CONTRACT", "").strip()
    if override:
        return load_contract(override)
    if not database_name:
        return None
    normalized = database_name.strip().lower()
    if normalized == "olist_ecommerce_demo":
        normalized = "olist"
    path = contracts_directory() / f"{normalized}.json"
    if path.is_file():
        return load_contract(path)
    return None
