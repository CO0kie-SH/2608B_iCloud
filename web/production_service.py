from __future__ import annotations

from typing import Any

from tools.config import settings_for_account
from tools.db import utc_now
from tools.production import produce_aliases
from web.jobs import ProductionJob, submit_production


def production_options_data(accounts: list[Any], *, settings: Any, db: Any) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for account in accounts:
        db.reconcile_cookie_flag(account)
        quota = db.get_create_quota(account.name)
        capacity = db.get_alias_capacity(account.name)
        flag = db.get_account_flag(account.name) or {}
        cookie_invalid = bool(flag.get("cookie_invalid")) or not bool(account.ok)
        alias_limit_reached = bool(flag.get("alias_limit_reached")) or not capacity.allowed
        items.append(
            {
                "name": account.name,
                "mail": account.mail,
                "icloud_domain": settings_for_account(settings, account).domain,
                "hme_ok": bool(account.ok) and not cookie_invalid and not flag.get("free_plan", False) and not alias_limit_reached,
                "free_plan": bool(flag.get("free_plan")),
                "plan_name": str(flag.get("plan_name") or ""),
                "plan_checked_at": int(flag.get("plan_checked_at") or 0),
                "cookie_invalid": cookie_invalid,
                "cookie_invalid_reason": flag.get("reason")
                or ("cookie_incomplete" if not account.ok else ""),
                "quota_used": quota.used,
                "quota_limit": quota.limit,
                "quota_remaining": quota.remaining,
                "quota_retry_after_sec": quota.retry_after_sec,
                "alias_count": capacity.alias_count,
                "alias_pending": capacity.pending,
                "alias_limit": capacity.limit,
                "alias_remaining": capacity.remaining,
                "alias_limit_reached": alias_limit_reached,
                "alias_limit_reason": str(flag.get("alias_limit_reason") or ""),
                "last_produce_at": quota.last_produce_at,
                "next_produce_at": quota.next_produce_at,
            }
        )
    return {
        "interfaces": [
            {
                "id": "legacy",
                "label": "旧版接口（每小时5个，间隔13-15分钟）",
                "limit": 5,
                "min_interval_sec": 13 * 60,
                "max_interval_sec": 15 * 60,
            }
        ],
        "accounts": items,
        "sync_at": utc_now(),
    }


def submit_account_production(
    account: Any,
    *,
    interface: str,
    count: int,
    threads: int,
    settings: Any,
    db: Any,
) -> ProductionJob:
    """提交单账号生产任务；普通生产页和轮询线程共用此入口。"""
    interface = str(interface or "legacy")
    count = int(count)
    threads = int(threads)
    if interface != "legacy":
        raise ValueError("未知生产接口")
    if count != 1:
        raise ValueError("旧版接口受 13-15 分钟间隔限制，单次只能生产 1 个")
    if threads < 1 or threads > 5:
        raise ValueError("并发线程范围为 1-5")

    db.reconcile_cookie_flag(account)
    db.assert_production_ready(account.name)
    db.assert_alias_capacity(account.name)
    db.assert_cookie_ready(account.name)
    db.assert_can_create(account.name)
    account_settings = settings_for_account(settings, account)

    def runner(on_progress: Any):
        return produce_aliases(
            account,
            settings=account_settings,
            db=db,
            count=count,
            threads=threads,
            on_progress=on_progress,
        )

    return submit_production(
        account_label=account.name,
        interface=interface,
        requested=count,
        db=db,
        runner=runner,
    )


__all__ = ["production_options_data", "submit_account_production"]
