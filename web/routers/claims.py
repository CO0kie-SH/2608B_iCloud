from __future__ import annotations

"""本地领取：确认数量 + 备注 + 发到常用邮箱。导出 邮箱----取码URL。"""

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request

from tools.claim_delivery import (
    build_claim_lines,
    deliver_claim_order,
    public_base_url,
)
from tools.code_lookup import find_latest_code, maybe_sync_account
from web.deps import get_accounts, get_db, get_mail_client, get_settings, resolve_account
from web.schemas import (
    ClaimOrderOut,
    ClaimStatsOut,
    claim_order_to_out,
)

router = APIRouter(prefix="/api/claims", tags=["claims"])


def _request_base(request: Request | None) -> str:
    if request is None:
        return ""
    try:
        return str(request.base_url).rstrip("/")
    except Exception:
        return ""


def _enrich_order(order: dict[str, Any], *, base_url: str) -> dict[str, Any]:
    data = dict(order or {})
    lines = build_claim_lines(data, base_url=base_url)
    items = []
    for item, line in zip(data.get("items") or [], lines):
        row = dict(item)
        hme = str(row.get("hme") or "").strip()
        token = str(row.get("access_token") or "").strip()
        code_url = ""
        if "----" in line:
            code_url = line.split("----", 1)[1].strip()
        row["code_url"] = code_url
        row["export_line"] = line if line else (f"{hme}----{token}" if token else hme)
        items.append(row)
    # items 比 lines 多/少时兜底
    if len(items) != len(data.get("items") or []):
        items = []
        for item in data.get("items") or []:
            row = dict(item)
            hme = str(row.get("hme") or "").strip()
            token = str(row.get("access_token") or "").strip()
            from tools.claim_delivery import build_code_url

            code_url = build_code_url(base_url, token, email=hme)
            row["code_url"] = code_url
            row["export_line"] = f"{hme}----{code_url}" if hme and code_url else hme
            items.append(row)
    data["items"] = items
    data["export_lines"] = [
        str(it.get("export_line") or "").strip()
        for it in items
        if str(it.get("export_line") or "").strip()
    ] or lines
    data["emails"] = [str(it.get("hme") or "").strip() for it in items if it.get("hme")]
    return data


def _pick_sender_account(preferred: str | None = None):
    """选一个能 SMTP 发信的账户；优先用户指定，否则找第一个 mail_ready。"""
    if preferred and str(preferred).strip():
        acc = resolve_account(str(preferred).strip())
        if not getattr(acc, "mail_ready", False):
            raise HTTPException(
                status_code=400,
                detail=f"发信账户不可用（缺 app_password / 收件配置）: {acc.name}",
            )
        return acc
    accounts = get_accounts()
    for acc in accounts:
        if getattr(acc, "mail_ready", False):
            return acc
    raise HTTPException(
        status_code=400,
        detail="没有可用的发信账户。请至少配置一个带 app_password 的 iCloud/163 账户。",
    )


@router.get("/stats", response_model=ClaimStatsOut)
def claim_stats() -> ClaimStatsOut:
    data = get_db().get_claim_stats()
    return ClaimStatsOut(**data)


@router.get("/orders", response_model=list[ClaimOrderOut])
def list_orders(
    request: Request,
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[ClaimOrderOut]:
    base = public_base_url(get_settings(), _request_base(request))
    rows = get_db().list_claim_orders(limit=limit, offset=offset)
    return [claim_order_to_out(_enrich_order(r, base_url=base)) for r in rows]


@router.get("/orders/{order_no}", response_model=ClaimOrderOut)
def get_order(order_no: str, request: Request) -> ClaimOrderOut:
    row = get_db().get_claim_order(order_no)
    if not row:
        raise HTTPException(status_code=404, detail=f"订单不存在: {order_no}")
    base = public_base_url(get_settings(), _request_base(request))
    return claim_order_to_out(_enrich_order(row, base_url=base))


@router.post("/checkout", response_model=ClaimOrderOut)
def checkout(
    request: Request,
    payload: dict[str, Any] = Body(default_factory=dict),
) -> ClaimOrderOut:
    """
    本地领取确认：
    - count: 数量（必填）
    - contact_email: 常用邮箱，领取列表会发到这里（必填）
    - note: 备注（可选，替代安全密码）
    - account: 可选，只从某个母号池子领
    - sender_account: 可选，指定用哪个账户 SMTP 发信
    - send_email: 默认 true；false 只占用不发信
    """
    try:
        count = int(payload.get("count") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="数量必须是整数") from exc

    contact_email = str(payload.get("contact_email") or payload.get("email") or "").strip()
    note = str(payload.get("note") or "").strip()
    account = str(payload.get("account") or "").strip() or None
    send_email = payload.get("send_email", True)
    if isinstance(send_email, str):
        send_email = send_email.strip().lower() not in {"0", "false", "no", "off"}

    db = get_db()
    base = public_base_url(get_settings(), _request_base(request))
    try:
        order = db.claim_aliases(
            count=count,
            contact_email=contact_email,
            note=note,
            account=account,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    order = _enrich_order(order, base_url=base)

    if not send_email:
        db.update_claim_delivery(
            order["order_no"],
            deliver_status="skipped",
            deliver_error="用户选择不发送邮件",
        )
        fresh = db.get_claim_order(order["order_no"]) or order
        return claim_order_to_out(_enrich_order(fresh, base_url=base))

    sender = _pick_sender_account(payload.get("sender_account"))
    try:
        client = get_mail_client(sender, timeout=30.0)
        result = deliver_claim_order(client, order, base_url=base)
        db.update_claim_delivery(
            order["order_no"],
            deliver_status=result["deliver_status"],
            deliver_from=result["deliver_from"],
            deliver_error="",
        )
    except Exception as exc:
        db.update_claim_delivery(
            order["order_no"],
            deliver_status="failed",
            deliver_error=f"{type(exc).__name__}: {exc}",
            deliver_from=str(getattr(sender, "mail", "") or getattr(sender, "name", "") or ""),
        )

    fresh = db.get_claim_order(order["order_no"]) or order
    return claim_order_to_out(_enrich_order(fresh, base_url=base))


@router.post("/orders/{order_no}/resend", response_model=ClaimOrderOut)
def resend_order(
    order_no: str,
    request: Request,
    payload: dict[str, Any] = Body(default_factory=dict),
) -> ClaimOrderOut:
    """对已领取订单重新发信到原常用邮箱。"""
    db = get_db()
    order = db.get_claim_order(order_no)
    if not order:
        raise HTTPException(status_code=404, detail=f"订单不存在: {order_no}")

    base = public_base_url(get_settings(), _request_base(request))
    order = _enrich_order(order, base_url=base)
    sender = _pick_sender_account(payload.get("sender_account"))
    try:
        client = get_mail_client(sender, timeout=30.0)
        result = deliver_claim_order(client, order, base_url=base)
        db.update_claim_delivery(
            order_no,
            deliver_status=result["deliver_status"],
            deliver_from=result["deliver_from"],
            deliver_error="",
        )
    except Exception as exc:
        db.update_claim_delivery(
            order_no,
            deliver_status="failed",
            deliver_error=f"{type(exc).__name__}: {exc}",
            deliver_from=str(getattr(sender, "mail", "") or getattr(sender, "name", "") or ""),
        )
        raise HTTPException(
            status_code=502,
            detail=f"发信失败: {type(exc).__name__}: {exc}",
        ) from exc

    fresh = db.get_claim_order(order_no)
    if not fresh:
        raise HTTPException(status_code=404, detail=f"订单不存在: {order_no}")
    return claim_order_to_out(_enrich_order(fresh, base_url=base))


# ---------- 注册机兼容取码 ----------
code_router = APIRouter(tags=["code"])


@code_router.get("/api/v1/code")
def lookup_code(
    token: str = Query(default=""),
    email: str = Query(default=""),
    after: float = Query(default=0),
    max_age_seconds: int = Query(default=600, ge=0, le=86400),
    allow_stale: bool = Query(default=False),
    sync: bool = Query(default=True),
    api_key: str = Query(default=""),
) -> dict[str, Any]:
    """
    注册机取码接口。
    凭证格式：邮箱----http://127.0.0.1:8770/api/v1/code?token=xxx
    也兼容 X-API-Key / api_key = token。
    """
    key = (token or api_key or "").strip()
    if not key:
        raise HTTPException(status_code=401, detail="missing token")

    db = get_db()
    item = None
    if email:
        item = db.get_claim_item_by_email_token(email, key)
    if item is None:
        item = db.get_claim_item_by_token(key)
    if item is None:
        raise HTTPException(status_code=404, detail="invalid token")

    hme = str(item.get("hme") or "").strip()
    if email and email.strip().lower() != hme.lower():
        raise HTTPException(status_code=404, detail="email/token mismatch")

    sync_info = {"synced": False}
    if sync:
        sync_info = maybe_sync_account(db, str(item.get("account") or ""))

    result = find_latest_code(
        db,
        email=hme,
        after=after,
        max_age_seconds=max_age_seconds,
        allow_stale=allow_stale,
    )
    result["sync"] = sync_info
    result["token_hint"] = key[:6] + "..." if len(key) > 6 else key
    return result
