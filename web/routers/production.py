from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Response, status

from web.deps import get_accounts, get_db, get_settings, resolve_account
from web.jobs import get_production_job, list_production_jobs
from web.production_service import production_options_data, submit_account_production

router = APIRouter(prefix="/api/production", tags=["production"])


@router.get("/options")
def production_options() -> dict[str, Any]:
    return production_options_data(
        get_accounts(), settings=get_settings(), db=get_db()
    )


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def start_production(
    response: Response,
    payload: dict[str, Any] = Body(default_factory=dict),
    account: str | None = Query(default=None),
) -> dict[str, Any]:
    acc = resolve_account(account or payload.get("account"))
    db = get_db()
    interface = str(payload.get("interface") or "legacy")
    count = int(payload.get("count") or 1)
    threads = int(payload.get("threads") or 1)
    job = submit_account_production(
        acc,
        interface=interface,
        count=count,
        threads=threads,
        settings=get_settings(),
        db=db,
    )
    response.headers["Location"] = f"/api/production/jobs/{job.job_id}"
    return job.to_dict()


@router.get("/jobs")
def production_jobs() -> dict[str, Any]:
    return {"items": list_production_jobs(get_db())}


@router.get("/jobs/{job_id}")
def production_job(job_id: str) -> dict[str, Any]:
    job = get_production_job(job_id, get_db())
    if not job:
        raise HTTPException(status_code=404, detail=f"生产任务不存在: {job_id}")
    return job.to_dict() if hasattr(job, "to_dict") else job
