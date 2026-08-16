from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Response, status

from web.deps import get_accounts, get_db, get_sync_service, resolve_account
from web.jobs import get_job, list_jobs, submit_sync
from web.schemas import SyncJobOut

router = APIRouter(prefix="/api/sync", tags=["sync"])


@router.post("", response_model=SyncJobOut, status_code=status.HTTP_202_ACCEPTED)
def start_sync(
    response: Response,
    payload: dict[str, Any] = Body(default_factory=dict),
    account: str | None = Query(default=None),
) -> SyncJobOut:
    """触发后台收信。立即返回 job_id，由前端轮询进度。"""
    all_accounts = bool(payload.get("all_accounts"))
    limit = int(payload.get("limit") or 200)
    full = bool(payload.get("full"))
    boxes = payload.get("mailboxes") or None

    if all_accounts:
        targets = get_accounts()
        if not targets:
            raise HTTPException(status_code=500, detail="未加载到任何账户")
        label = f"{len(targets)} 个账户"
    else:
        acc = resolve_account(account or payload.get("account"))
        targets = [acc]
        label = acc.name

    service = get_sync_service()

    def runner(on_progress):
        return service.sync_accounts(
            targets,
            mailboxes=boxes,
            limit=limit,
            full=full,
            skip_unready=all_accounts,
            on_progress=on_progress,
        )

    job = submit_sync(account_label=label, runner=runner, deduplicate=True)
    response.headers["Location"] = f"/api/sync/{job.job_id}"
    return SyncJobOut(**job.to_dict())


@router.get("/state")
def sync_state(account: str | None = Query(default=None)) -> dict[str, Any]:
    """各账户/目录的收取水位。"""
    return {"items": get_db().list_sync_states(account)}


@router.get("/jobs")
def all_jobs() -> dict[str, Any]:
    return {"items": list_jobs()}


@router.get("/{job_id}", response_model=SyncJobOut)
def job_status(job_id: str) -> SyncJobOut:
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")
    return SyncJobOut(**job.to_dict())
