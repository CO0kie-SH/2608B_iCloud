from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Body, Request

from tools.db import utc_now
from web.deps import get_accounts, get_db, get_sync_service
from web.jobs import submit_sync

router = APIRouter(prefix="/api/client-sync", tags=["client-sync"])


@router.post("/open")
def client_open(
    request: Request, payload: dict[str, Any] = Body(default_factory=dict)
) -> dict[str, Any]:
    """客户端打开时登记、返回中央状态，并去重触发一次全账号收信。"""
    client_id = str(payload.get("client_id") or "").strip() or uuid.uuid4().hex
    db = get_db()
    seen_at = db.touch_client(client_id, request.headers.get("user-agent", ""))
    accounts = get_accounts()
    job = None
    if bool(payload.get("auto_mail_sync", True)) and accounts:
        service = get_sync_service()

        def runner(on_progress):
            return service.sync_accounts(accounts, limit=200, on_progress=on_progress)

        job = submit_sync(
            account_label=f"全部 {len(accounts)} 个账户",
            runner=runner,
            deduplicate=True,
        )

    quotas = {}
    for account in accounts:
        quota = db.get_create_quota(account.name)
        quotas[account.name] = {
            "used": quota.used,
            "limit": quota.limit,
            "remaining": quota.remaining,
            "retry_after_sec": quota.retry_after_sec,
        }
    return {
        "client_id": client_id,
        "seen_at": seen_at,
        "sync_at": utc_now(),
        "mail_job": job.to_dict() if job else None,
        "quotas": quotas,
        "production_jobs": db.list_production_jobs(limit=50),
        "endpoints": {
            "accounts": "/api/accounts",
            "aliases": "/api/aliases",
            "mails": "/api/mails",
            "production_jobs": "/api/production/jobs",
        },
    }
