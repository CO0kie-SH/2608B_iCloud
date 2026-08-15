from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Response, status

from tools.db import utc_now
from tools.production import produce_aliases
from web.deps import get_accounts, get_db, get_settings, resolve_account
from web.jobs import get_production_job, list_production_jobs, submit_production

router = APIRouter(prefix="/api/production", tags=["production"])


@router.get("/options")
def production_options() -> dict[str, Any]:
    db = get_db()
    accounts = []
    for acc in get_accounts():
        quota = db.get_create_quota(acc.name)
        flag = db.get_account_flag(acc.name) or {}
        cookie_invalid = bool(flag.get("cookie_invalid")) or not bool(acc.ok)
        accounts.append(
            {
                "name": acc.name,
                "mail": acc.mail,
                "hme_ok": bool(acc.ok) and not cookie_invalid,
                "cookie_invalid": cookie_invalid,
                "cookie_invalid_reason": flag.get("reason") or ("cookie_incomplete" if not acc.ok else ""),
                "quota_used": quota.used,
                "quota_limit": quota.limit,
                "quota_remaining": quota.remaining,
                "quota_retry_after_sec": quota.retry_after_sec,
                "last_produce_at": quota.last_produce_at,
                "next_produce_at": quota.next_produce_at,
            }
        )
    return {
        "interfaces": [
            {
                "id": "legacy",
                "label": "旧版接口（每小时5个，间隔13–15分钟）",
                "limit": 5,
                "min_interval_sec": 13 * 60,
                "max_interval_sec": 15 * 60,
            }
        ],
        "accounts": accounts,
        "sync_at": utc_now(),
    }


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def start_production(
    response: Response,
    payload: dict[str, Any] = Body(default_factory=dict),
    account: str | None = Query(default=None),
) -> dict[str, Any]:
    acc = resolve_account(account or payload.get("account"))
    db = get_db()
    db.assert_cookie_ready(acc.name)
    interface = str(payload.get("interface") or "legacy")
    if interface != "legacy":
        raise HTTPException(status_code=400, detail="未知生产接口")
    count = int(payload.get("count") or 1)
    threads = int(payload.get("threads") or 1)
    if count < 1 or count > 1:
        raise HTTPException(status_code=400, detail="旧版接口受 13–15 分钟间隔限制，单次只能生产 1 个")
    if threads < 1 or threads > 5:
        raise HTTPException(status_code=400, detail="并发线程范围为 1-5")
    settings = get_settings()

    def runner(on_progress):
        return produce_aliases(
            acc,
            settings=settings,
            db=db,
            count=count,
            threads=threads,
            on_progress=on_progress,
        )

    job = submit_production(
        account_label=acc.name, interface=interface, requested=count, db=db, runner=runner
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
