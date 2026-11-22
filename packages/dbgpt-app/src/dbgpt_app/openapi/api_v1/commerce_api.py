"""Local DataTrail diagnostic page API; never accepts filesystem paths or SQL."""

import asyncio
import os
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from dbgpt_app.commerce_contract import resolve_contract_for_database
from dbgpt_app.commerce_memory import MetricMemoryError, default_memory
from dbgpt_serve.utils.auth import UserRequest, get_user_from_headers

from .tools.commerce_profile import profile_path

router = APIRouter()


@router.get("/v1/commerce/profile")
async def commerce_profile():
    configured_path = os.environ.get("DATATRAIL_DATABASE")
    if not configured_path:
        raise HTTPException(503, "请在启动配置中设置 DATATRAIL_DATABASE。")
    try:
        path = Path(configured_path).resolve(strict=True)
        report = await asyncio.to_thread(profile_path, path)
    except (OSError, ValueError, sqlite3.Error):
        raise HTTPException(503, "Olist 数据源暂不可用或不符合已支持的表结构。")
    return {
        "data_source": "olist_ecommerce_demo",
        "guard_enabled": os.environ.get("DATATRAIL_GRAIN_GUARD") == "1",
        "model_configured": bool(os.environ.get("OPENAI_API_KEY"))
        and not os.environ["OPENAI_API_KEY"].startswith("not-configured"),
        "profile": report,
    }


def _memory_owner(user_token: UserRequest | None):
    """Metric memory belongs to one authenticated user; anonymous callers get none."""
    memory = default_memory()
    if memory is None:
        raise HTTPException(404, "口径记忆未启用")
    owner = ((user_token.user_id if user_token else "") or "").strip()
    if not owner:
        raise HTTPException(401, "需要登录用户")
    return memory, owner


@router.get("/v1/datatrail/metric-memory")
async def list_metric_memory(
    database: str = Query(..., min_length=1),
    user_token: UserRequest = Depends(get_user_from_headers),
):
    memory, owner = _memory_owner(user_token)
    return memory.records(owner, database)


@router.post("/v1/datatrail/metric-memory/{memory_id}/confirm")
async def confirm_metric_memory(
    memory_id: int, user_token: UserRequest = Depends(get_user_from_headers)
):
    memory, owner = _memory_owner(user_token)
    try:
        database = memory.get(memory_id, owner)["database"]
        return memory.confirm(memory_id, owner, resolve_contract_for_database(database))
    except MetricMemoryError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/v1/datatrail/metric-memory/{memory_id}/revoke")
async def revoke_metric_memory(
    memory_id: int, user_token: UserRequest = Depends(get_user_from_headers)
):
    memory, owner = _memory_owner(user_token)
    try:
        return memory.revoke(memory_id, owner)
    except MetricMemoryError as exc:
        raise HTTPException(409, str(exc)) from exc
