from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from web.deps import get_db, get_mail_client, resolve_account
from tools.mail import MAILBOX_LABELS, MAILBOX_ORDER, mailbox_label, mailbox_role
from web.schemas import (
    MAIL_TYPE_LABELS,
    MAIL_TYPE_ORDER,
    MailDetailOut,
    MailListOut,
    MailStatsOut,
    mail_to_out,
)

router = APIRouter(prefix="/api/mails", tags=["mails"])

# 详情页正文实时拉取：单独用较短超时，避免页面长时间转圈
DETAIL_TIMEOUT = 20.0
DETAIL_BODY_LIMIT = 200000


@router.get("", response_model=MailListOut)
def list_mails(
    account: str | None = Query(default=None),
    alias: str | None = Query(default=None),
    type: str | None = Query(default=None),
    mailbox: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> MailListOut:
    db = get_db()
    filters = dict(account=account, alias_hme=alias, mail_type=type, mailbox=mailbox, q=q)
    records = db.list_mails(limit=limit, offset=offset, **filters)
    return MailListOut(
        total=db.count_mails(**filters),
        limit=limit,
        offset=offset,
        items=[mail_to_out(r) for r in records],
    )


@router.get("/stats", response_model=MailStatsOut)
def mail_stats(
    account: str | None = Query(default=None),
    alias: str | None = Query(default=None),
    mailbox: str | None = Query(default=None),
    q: str | None = Query(default=None),
) -> MailStatsOut:
    db = get_db()
    by_type = db.count_mails_by_type(account, alias, mailbox=mailbox, q=q)
    # 旧版本可能已经把 163 的 modified UTF-7 箱名写进数据库；统计接口
    # 对外统一用逻辑键，避免「垃圾邮件」和「Junk」拆成两个分组。
    raw_by_mailbox = db.count_mails_by_mailbox(account, alias, q=q)
    by_mailbox: dict[str, int] = {}
    for raw_box, count in raw_by_mailbox.items():
        key = mailbox_role(raw_box) or "INBOX"
        by_mailbox[key] = by_mailbox.get(key, 0) + int(count)
    labels = dict(MAILBOX_LABELS)
    for box in by_mailbox:
        if box not in labels:
            labels[box] = mailbox_label(box)
    order = list(MAILBOX_ORDER)
    for box in by_mailbox:
        if box not in order:
            order.append(box)
    return MailStatsOut(
        total=sum(by_type.values()),
        by_type=by_type,
        type_order=list(MAIL_TYPE_ORDER),
        type_labels=dict(MAIL_TYPE_LABELS),
        by_mailbox=by_mailbox,
        mailbox_order=order,
        mailbox_labels=labels,
    )


@router.get("/{account}/{mailbox}/{uid}", response_model=MailDetailOut)
def mail_detail(account: str, mailbox: str, uid: str) -> MailDetailOut:
    """
    元数据取自本地库，正文实时从 IMAP 拉取（按设计不落库）。
    拉取失败时降级为只返回元数据 + fetch_error，前端仍可展示分类结果。
    """
    db = get_db()
    record = db.get_mail(account, mailbox, uid)
    if not record:
        raise HTTPException(status_code=404, detail=f"邮件不存在: {account}/{mailbox}/{uid}")

    meta = mail_to_out(record)
    acc = resolve_account(account)
    try:
        client = get_mail_client(acc, timeout=DETAIL_TIMEOUT)
        data = client.get_by_uid(
            uid=uid, mailbox=mailbox, include_body=True, body_limit=DETAIL_BODY_LIMIT
        )
    except HTTPException as e:
        return MailDetailOut(meta=meta, fetch_error=str(e.detail))
    except Exception as e:
        return MailDetailOut(meta=meta, fetch_error=f"{type(e).__name__}: {e}")

    envelope = (data.get("envelope_from") or "").strip()
    if envelope and envelope != (meta.envelope_from or ""):
        meta.envelope_from = envelope
        try:
            db.upsert_mail(
                account=record.account,
                uid=record.uid,
                mailbox=record.mailbox,
                parent_mail=record.parent_mail,
                message_id=record.message_id,
                alias_hme=record.alias_hme,
                from_name=record.from_name,
                from_addr=record.from_addr,
                sender_addr=record.sender_addr,
                return_path=record.return_path,
                received_spf=data.get("received_spf") or record.received_spf,
                envelope_from=envelope,
                is_relayed=bool(record.is_relayed),
                relay_label=record.relay_label,
                to_addr=record.to_addr,
                delivered_to=record.delivered_to,
                subject=record.subject,
                mail_type=record.mail_type,
                code=record.code,
                summary=record.summary,
                date_header=record.date_header,
                date_utc=record.date_utc,
                internaldate=record.internaldate,
                size=record.size,
                is_seen=bool(record.is_seen),
                body_text_len=record.body_text_len,
                body_html_len=record.body_html_len,
                content_type=record.content_type,
            )
        except Exception:
            pass

    return MailDetailOut(
        meta=meta,
        body_text=data.get("body_text") or "",
        body_html=data.get("body_html") or "",
    )
