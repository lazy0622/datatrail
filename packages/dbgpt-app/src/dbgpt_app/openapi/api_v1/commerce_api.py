"""Local DataTrail diagnostic page API; never accepts filesystem paths or SQL."""

import asyncio
import os
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException

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
