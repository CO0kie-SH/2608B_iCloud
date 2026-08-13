from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from tools.client import ICloudHMEClient
from tools.hme import HMEService
from web.deps import get_db, get_settings, parent_mail_of, resolve_account
from web.schemas import AliasOut, alias_to_out

router = APIRouter(prefix="/api/aliases", tags=["aliases"])


@router.get("", response_model=list[AliasOut])
def list_aliases(
    account: str | None = Query(default=None),
    q: str | None = Query(default=None),
    active: bool | None = Query(default=None),
    group_id: int | None = Query(default=None),
) -> list[AliasOut]:
    """邮箱池子：别名 + 邮件数 + 最近来信时间 + 所属分组。"""
    db = get_db()
    records = db.list_aliases(account=account)
    mail_stats = db.count_mails_by_alias(account=account)
    groups_map = db.group_members_map()

    if active is not None:
        records = [r for r in records if bool(r.is_active) == active]
    if q:
        needle = q.strip().lower()
        records = [
            r
            for r in records
            if needle in (r.hme or "").lower()
            or needle in (r.label or "").lower()
            or needle in (r.cdk or "").lower()
            or needle in (r.note or "").lower()
        ]
    if group_id is not None:
        allowed = {
            hme
            for hme, groups in groups_map.items()
            if any(int(g["id"]) == int(group_id) for g in groups)
        }
        records = [r for r in records if r.hme in allowed]

    return [
        alias_to_out(r, mail_stats=mail_stats, groups_map=groups_map) for r in records
    ]


@router.post("/{anonymous_id}/active")
def set_alias_active(
    anonymous_id: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    account: str | None = Query(default=None),
) -> dict[str, Any]:
    """启停别名转发。需要有效 cookie。"""
    active = bool(payload.get("active", True))
    acc = resolve_account(account or payload.get("account"))
    if not acc.ok:
        raise HTTPException(
            status_code=409,
            detail=f"[{acc.name}] cookie 不完整或已失效，请先在命令行运行 cookie-login",
        )
    settings = get_settings()
    with ICloudHMEClient(settings, acc.cookies) as client:
        svc = HMEService(client)
        res = svc.set_active(anonymous_id, active)

    db = get_db()
    db.set_active(acc.name, anonymous_id, active)
    return {"ok": True, "anonymous_id": anonymous_id, "active": active, "result": res}


@router.post("/refresh")
def refresh_aliases(
    payload: dict[str, Any] = Body(default_factory=dict),
    account: str | None = Query(default=None),
) -> dict[str, Any]:
    """从 iCloud 拉取别名列表并同步进本地库（等价于 CLI 的 list）。"""
    acc = resolve_account(account or payload.get("account"))
    if not acc.ok:
        raise HTTPException(
            status_code=409,
            detail=f"[{acc.name}] cookie 不完整或已失效，请先在命令行运行 cookie-login",
        )
    settings = get_settings()
    with ICloudHMEClient(settings, acc.cookies) as client:
        aliases = HMEService(client).list_aliases()

    db = get_db()
    for a in aliases:
        db.upsert_alias(
            account=acc.name,
            parent_mail=parent_mail_of(acc),
            hme=a.hme,
            label=a.label,
            cdk=a.label if str(a.label).startswith("CDK_") else None,
            anonymous_id=a.anonymous_id,
            is_active=a.is_active,
            create_timestamp=a.create_timestamp,
            source="list",
            raw=a.raw,
        )
    return {"ok": True, "account": acc.name, "count": len(aliases)}
