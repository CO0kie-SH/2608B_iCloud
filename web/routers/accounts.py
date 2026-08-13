from __future__ import annotations

from fastapi import APIRouter, Query

from tools.db import AliasDB
from web.deps import get_accounts, get_db, parent_mail_of
from web.schemas import AccountOut

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


def _account_out(acc, db: AliasDB) -> AccountOut:
    parent = parent_mail_of(acc)
    quota = db.get_create_quota(acc.name)
    inbox = acc.resolve_inbox()
    return AccountOut(
        name=acc.name,
        mail=acc.mail or "",
        apple_id=acc.apple_id or "",
        hme_ok=bool(acc.ok),
        mail_ready=bool(acc.mail_ready),
        providers=sorted(acc.providers.keys()),
        # 收件端点未必是母号的 iCloud 地址（可能配了 163），前端要显示真实来源
        inbox_provider=(inbox.name if inbox else ""),
        inbox_mail=(inbox.mail if inbox else ""),
        alias_count=len(db.list_aliases(acc.name)),
        mail_count=db.count_mails(account=acc.name),
        quota_used=quota.used,
        quota_limit=quota.limit,
        quota_remaining=quota.remaining,
        quota_retry_after_sec=quota.retry_after_sec,
        format_errors=list(acc.format_errors or []),
        missing_cookie_keys=list(acc.missing or []),
    )


@router.get("", response_model=list[AccountOut])
def list_accounts(account: str | None = Query(default=None)) -> list[AccountOut]:
    db = get_db()
    accounts = get_accounts()
    if account:
        accounts = [a for a in accounts if a.name == account]
    return [_account_out(a, db) for a in accounts]
