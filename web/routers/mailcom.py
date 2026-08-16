from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from tools.mailcom import MailComAccount, MailComClient, MailComHeader, load_mailcom_accounts, message_payload, normalize_message
from web.deps import get_settings
from web.schemas import (
    MAIL_TYPE_LABELS,
    MAIL_TYPE_ORDER,
    MailComAccountOut,
    MailComDetailOut,
    MailComListOut,
    MailComMessageOut,
    MailComStatsOut,
)

router = APIRouter(prefix="/api/mailcom", tags=["mail.com"])
MAILCOM_TIMEOUT = 5


def _accounts() -> list[MailComAccount]:
    return load_mailcom_accounts(get_settings().base_dir)


def _resolve_account(email: str) -> MailComAccount:
    key = (email or "").strip().lower()
    for account in _accounts():
        if account.email.lower() == key:
            return account
    available = ", ".join(item.email for item in _accounts()) or "(none)"
    raise HTTPException(status_code=404, detail=f"未找到 mail.com 账号: {email}（可用: {available}）")


def _message_out(payload: dict) -> MailComMessageOut:
    return MailComMessageOut(
        id=str(payload.get("id") or ""),
        account=str(payload.get("account") or ""),
        subject=str(payload.get("subject") or ""),
        sender=str(payload.get("sender") or ""),
        from_name=str(payload.get("from_name") or ""),
        from_addr=str(payload.get("from_addr") or ""),
        date_header=str(payload.get("date_header") or ""),
        date_utc=str(payload.get("date_utc") or ""),
        timestamp=payload.get("timestamp"),
        mail_type=str(payload.get("mail_type") or "other"),
        code=str(payload.get("code") or ""),
        summary=str(payload.get("summary") or ""),
        body_text_len=int(payload.get("body_text_len") or 0),
        body_html_len=int(payload.get("body_html_len") or 0),
        has_body=bool(payload.get("has_body")),
    )


def _load_messages(
    account: MailComAccount,
    limit: int,
    query: str = "",
    *,
    include_body: bool = False,
) -> list[dict]:
    client = MailComClient(account, request_timeout=MAILCOM_TIMEOUT)
    wanted = (query or "").strip().lower()
    results: list[dict] = []
    for header in client.messages(limit):
        try:
            payload = message_payload(client, account, header, include_body=include_body)
        except Exception:
            payload = normalize_message(account, header)
        if wanted:
            haystack = " ".join(
                str(payload.get(key) or "")
                for key in ("subject", "sender", "from_addr", "summary", "code", "body_text")
            ).lower()
            if wanted not in haystack:
                continue
        results.append(payload)
    return results


@router.get("/accounts", response_model=list[MailComAccountOut])
def list_mailcom_accounts() -> list[MailComAccountOut]:
    return [
        MailComAccountOut(
            email=item.email,
            source=Path(item.source).name if item.source else "",
            ready=bool(item.password),
        )
        for item in _accounts()
    ]


@router.get("/messages/stats", response_model=MailComStatsOut)
def mailcom_stats(
    account: str = Query(...),
    q: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
) -> MailComStatsOut:
    try:
        records = _load_messages(_resolve_account(account), limit, q or "", include_body=bool(q))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"mail.com 收件失败: {type(exc).__name__}: {exc}") from exc
    by_type: dict[str, int] = {}
    for record in records:
        kind = str(record.get("mail_type") or "other")
        by_type[kind] = by_type.get(kind, 0) + 1
    return MailComStatsOut(
        total=len(records),
        by_type=by_type,
        type_order=list(MAIL_TYPE_ORDER),
        type_labels=dict(MAIL_TYPE_LABELS),
    )


@router.get("/messages", response_model=MailComListOut)
def list_mailcom_messages(
    account: str = Query(...),
    q: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
) -> MailComListOut:
    try:
        records = _load_messages(_resolve_account(account), limit, q or "", include_body=bool(q))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"mail.com 收件失败: {type(exc).__name__}: {exc}") from exc
    by_type: dict[str, int] = {}
    for record in records:
        kind = str(record.get("mail_type") or "other")
        by_type[kind] = by_type.get(kind, 0) + 1
    return MailComListOut(
        total=len(records),
        limit=limit,
        items=[_message_out(record) for record in records],
        by_type=by_type,
        type_order=list(MAIL_TYPE_ORDER),
        type_labels=dict(MAIL_TYPE_LABELS),
    )


@router.get("/messages/{account}/{mail_id}", response_model=MailComDetailOut)
def mailcom_detail(account: str, mail_id: str) -> MailComDetailOut:
    acc = _resolve_account(account)
    client = MailComClient(acc, request_timeout=MAILCOM_TIMEOUT)
    # The body endpoint accepts the mailbox identifier directly. Avoid a second
    # full list request here; the browser already has the list metadata.
    header = MailComHeader(id=str(mail_id), subject="", sender="")
    try:
        payload = message_payload(client, acc, header, include_body=True)
        return MailComDetailOut(
            meta=_message_out(payload),
            body_text=str(payload.get("body_text") or ""),
            body_html=str(payload.get("body_html") or ""),
        )
    except Exception as exc:
        payload = normalize_message(acc, header)
        return MailComDetailOut(
            meta=_message_out(payload),
            fetch_error=f"{type(exc).__name__}: {exc}",
        )


__all__ = ["router"]
