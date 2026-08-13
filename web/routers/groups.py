from __future__ import annotations

"""别名分组：本期只提供后端 CRUD，前端 UI 留占位，后续再做。"""

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from web.deps import get_db
from web.schemas import GroupOut

router = APIRouter(prefix="/api/groups", tags=["groups"])


@router.get("", response_model=list[GroupOut])
def list_groups(account: str | None = Query(default=None)) -> list[GroupOut]:
    rows = get_db().list_groups(account)
    return [
        GroupOut(
            id=r["id"],
            name=r["name"],
            account=r["account"] or "",
            color=r["color"] or "",
            note=r["note"] or "",
            member_count=int(r.get("member_count") or 0),
        )
        for r in rows
    ]


@router.post("", response_model=GroupOut)
def create_group(payload: dict[str, Any] = Body(...)) -> GroupOut:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="分组名称不能为空")
    row = get_db().create_group(
        name,
        account=str(payload.get("account") or ""),
        color=str(payload.get("color") or ""),
        note=str(payload.get("note") or ""),
    )
    if not row:
        raise HTTPException(status_code=500, detail="创建分组失败")
    return GroupOut(
        id=row["id"],
        name=row["name"],
        account=row["account"] or "",
        color=row["color"] or "",
        note=row["note"] or "",
    )


@router.delete("/{group_id}")
def delete_group(group_id: int) -> dict[str, Any]:
    if not get_db().delete_group(group_id):
        raise HTTPException(status_code=404, detail=f"分组不存在: {group_id}")
    return {"ok": True, "id": group_id}


@router.post("/{group_id}/members")
def add_member(group_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    hme = str(payload.get("hme") or "").strip()
    if not hme:
        raise HTTPException(status_code=400, detail="hme 不能为空")
    added = get_db().add_group_member(group_id, hme)
    return {"ok": True, "group_id": group_id, "hme": hme, "added": added}


@router.delete("/{group_id}/members/{hme}")
def remove_member(group_id: int, hme: str) -> dict[str, Any]:
    removed = get_db().remove_group_member(group_id, hme)
    if not removed:
        raise HTTPException(
            status_code=404, detail=f"分组 {group_id} 中无成员 {hme}"
        )
    return {"ok": True, "group_id": group_id, "hme": hme}
