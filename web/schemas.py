from __future__ import annotations

"""
Web 响应模型。

所有字段显式白名单声明：账户 YAML 里含 cookie 与 app_password，
绝不允许出现在任何响应里。
"""

from typing import Any, Literal

from pydantic import BaseModel

from tools.mail import MAILBOX_LABELS, MAILBOX_ORDER, mailbox_label

# 邮件分类的展示顺序与中文名（前端分块用，与 MailMessageParser.classify_type 对齐）
MAIL_TYPE_ORDER: tuple[str, ...] = (
    "code",
    "welcome",
    "invite",
    "security",
    "billing",
    "promo",
    "other",
)

MAIL_TYPE_LABELS: dict[str, str] = {
    "code": "验证码",
    "welcome": "欢迎",
    "invite": "邀请",
    "security": "安全",
    "billing": "账单",
    "promo": "促销",
    "other": "其他",
}


class AccountOut(BaseModel):
    name: str
    mail: str
    apple_id: str
    hme_ok: bool
    mail_ready: bool
    providers: list[str]
    inbox_provider: str
    inbox_mail: str
    alias_count: int
    mail_count: int
    quota_used: int
    quota_limit: int
    quota_remaining: int
    quota_retry_after_sec: int
    last_produce_at: int = 0
    next_produce_at: int = 0
    cookie_invalid: bool = False
    cookie_invalid_reason: str = ""
    format_errors: list[str]
    missing_cookie_keys: list[str]


class GroupOut(BaseModel):
    id: int
    name: str
    account: str
    color: str
    note: str
    member_count: int = 0


class AliasOut(BaseModel):
    id: int
    hme: str
    label: str
    cdk: str
    account: str
    parent_mail: str
    anonymous_id: str
    is_active: bool
    note: str
    source: str
    created_at: str
    mail_count: int = 0
    last_mail_at: str = ""
    groups: list[dict[str, Any]] = []


class MailOut(BaseModel):
    id: int
    account: str
    mailbox: str
    mailbox_label: str = ""
    uid: str
    alias_hme: str
    from_name: str
    from_addr: str
    sender_addr: str
    return_path: str
    received_spf: str = ""
    envelope_from: str = ""
    is_relayed: bool
    relay_label: str
    to_addr: str
    delivered_to: str
    subject: str
    mail_type: str
    code: str
    summary: str
    date_header: str
    date_utc: str
    internaldate: str
    size: int | None = None
    is_seen: bool = False
    has_attachment: bool = False
    attachments: list[dict[str, Any]] = []
    body_text_len: int = 0
    body_html_len: int = 0


class MailListOut(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[MailOut]


class MailStatsOut(BaseModel):
    total: int
    by_type: dict[str, int]
    type_order: list[str]
    type_labels: dict[str, str]
    by_mailbox: dict[str, int] = {}
    mailbox_order: list[str] = list(MAILBOX_ORDER)
    mailbox_labels: dict[str, str] = dict(MAILBOX_LABELS)


class MailDetailOut(BaseModel):
    meta: MailOut
    body_text: str = ""
    body_html: str = ""
    fetch_error: str = ""


class SyncJobOut(BaseModel):
    job_id: str
    status: str
    account: str = ""
    started_at: str = ""
    finished_at: str = ""
    progress: list[str] = []
    stats: list[dict[str, Any]] = []
    error: str = ""


class ProductionLoopConfigIn(BaseModel):
    selected_accounts: list[str] = []
    interface: Literal["legacy"] = "legacy"
    mode: Literal["forever", "timed"] = "forever"
    duration_minutes: int = 0
    interval_sec: int = 2


def alias_to_out(
    rec: Any,
    *,
    mail_stats: dict[str, dict[str, Any]] | None = None,
    groups_map: dict[str, list[dict[str, Any]]] | None = None,
) -> AliasOut:
    stats = (mail_stats or {}).get(rec.hme, {})
    return AliasOut(
        id=rec.id,
        hme=rec.hme,
        label=rec.label or "",
        cdk=rec.cdk or "",
        account=rec.account or "",
        parent_mail=rec.parent_mail or rec.account or "",
        anonymous_id=rec.anonymous_id or "",
        is_active=bool(rec.is_active),
        note=rec.note or "",
        source=rec.source or "",
        created_at=rec.created_at or "",
        mail_count=int(stats.get("count") or 0),
        last_mail_at=str(stats.get("last_mail_at") or ""),
        groups=(groups_map or {}).get(rec.hme, []),
    )


def mail_to_out(rec: Any) -> MailOut:
    d = rec.to_dict()
    return MailOut(
        id=d["id"],
        account=d["account"],
        mailbox=d["mailbox"],
        mailbox_label=mailbox_label(d["mailbox"]),
        uid=d["uid"],
        alias_hme=d["alias_hme"],
        from_name=d["from_name"],
        from_addr=d["from_addr"],
        sender_addr=d["sender_addr"],
        return_path=d["return_path"],
        received_spf=d.get("received_spf") or "",
        envelope_from=d.get("envelope_from") or "",
        is_relayed=d["is_relayed"],
        relay_label=d["relay_label"],
        to_addr=d["to_addr"],
        delivered_to=d["delivered_to"],
        subject=d["subject"],
        mail_type=d["mail_type"],
        code=d["code"],
        summary=d["summary"],
        date_header=d["date_header"],
        date_utc=d["date_utc"],
        internaldate=d["internaldate"],
        size=d["size"],
        is_seen=d["is_seen"],
        has_attachment=d["has_attachment"],
        attachments=d["attachments"],
        body_text_len=d["body_text_len"],
        body_html_len=d["body_html_len"],
    )
