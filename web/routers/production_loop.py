from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from web.deps import get_accounts, get_db, get_settings, resolve_account
from web.production_loop import get_production_loop_controller
from web.production_service import production_options_data
from web.schemas import ProductionLoopConfigIn

router = APIRouter(prefix="/api/production-loop", tags=["production-loop"])


def _snapshot() -> dict[str, Any]:
    controller = get_production_loop_controller()
    result = controller.snapshot()
    options = production_options_data(
        get_accounts(), settings=get_settings(), db=get_db()
    )
    result["accounts"] = options["accounts"]
    result["interfaces"] = options["interfaces"]
    result["sync_at"] = options["sync_at"]
    return result


@router.get("")
def loop_status() -> dict[str, Any]:
    return _snapshot()


@router.put("/config")
def update_loop_config(payload: ProductionLoopConfigIn) -> dict[str, Any]:
    get_production_loop_controller().configure(**payload.model_dump())
    return _snapshot()


@router.post("/start")
def start_loop() -> dict[str, Any]:
    get_production_loop_controller().start()
    return _snapshot()


@router.post("/stop")
def stop_loop() -> dict[str, Any]:
    get_production_loop_controller().stop()
    return _snapshot()


@router.post("/accounts/{account}/unlock")
def unlock_account(account: str) -> dict[str, Any]:
    """清除免费 5 GB 标记，恢复该账号的生产勾选能力。"""
    resolved = resolve_account(account)
    get_db().clear_account_free_plan(resolved.name)
    return _snapshot()
