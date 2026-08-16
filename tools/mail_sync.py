from __future__ import annotations

"""
邮件增量收取 → 分类 → 入库。

只持久化元数据与分类结果（发件人 / 代发人 / 收件别名 / 类型 / 验证码），
正文在详情页按需实时拉取，因此本模块拉取时需要正文来做分类与验证码提取，
算完即丢，不写入数据库。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .db import AliasDB
from .mail import ICloudMailClient, mail_client_from_account, mailbox_label

# 只扫「收件」相关目录；Sent/Drafts/Deleted 对展示无意义。
# 验证码邮件常被投进垃圾箱，所以 Junk 必须扫。
DEFAULT_SYNC_FOLDERS = ("INBOX", "Junk")

# 拉取时需要正文做分类，但不入库，因此截断以控制内存与耗时
SYNC_BODY_LIMIT = 20000


@dataclass
class SyncStats:
    account: str
    mailbox: str
    fetched: int = 0
    saved: int = 0
    updated: int = 0
    matched_alias: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    last_uid: int = 0
    ok: bool = True
    error: str = ""
    skipped: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "mailbox": self.mailbox,
            "fetched": self.fetched,
            "saved": self.saved,
            "updated": self.updated,
            "matched_alias": self.matched_alias,
            "by_type": dict(self.by_type),
            "last_uid": self.last_uid,
            "ok": self.ok,
            "error": self.error,
            "skipped": self.skipped,
            "note": self.note,
        }


def resolve_sync_folders(
    client: ICloudMailClient, requested: Iterable[str] | None = None
) -> list[str]:
    """
    确定要扫描的目录，返回逻辑名（INBOX / Junk）。

    163 垃圾箱不叫 Junk，LIST 出来是 modified UTF-7 的「垃圾邮件」。
    这里按 SPECIAL-USE 角色挑箱，SELECT 时再由 client.resolve_mailbox 翻成真实名。
    """
    if requested:
        return [str(f).strip() for f in requested if str(f).strip()]

    wanted = set(DEFAULT_SYNC_FOLDERS)
    picked: list[str] = []
    seen: set[str] = set()
    try:
        catalog = client.describe_folders()
    except Exception:
        catalog = []

    for folder in catalog:
        if not folder.selectable:
            continue
        role = folder.role or ""
        if role in wanted and role not in seen:
            picked.append(role)
            seen.add(role)

    if not picked:
        available = tuple(client.profile.folders or ())
        picked = [f for f in available if f in wanted]
    return picked or ["INBOX"]


class MailSyncService:
    """把「拉取 → 解析 → 别名反查 → 分类 → 入库」串起来。"""

    def __init__(self, db: AliasDB, *, timeout: float = 30.0) -> None:
        self.db = db
        self.timeout = timeout

    def _alias_index(self, account: str, parent_mail: str) -> dict[str, str]:
        """本地别名表里属于该母号的 hme 集合（小写 → 原值）。"""
        index: dict[str, str] = {}
        for key in {account, parent_mail}:
            if not key:
                continue
            for rec in self.db.list_aliases(key):
                if rec.hme:
                    index[rec.hme.strip().lower()] = rec.hme
        return index

    @staticmethod
    def _match_alias(item: dict[str, Any], alias_index: dict[str, str]) -> str:
        """按投递头优先级找出本地已知的收件别名。"""
        if not alias_index:
            return ""
        for cand in item.get("alias_candidates") or []:
            hit = alias_index.get(str(cand).strip().lower())
            if hit:
                return hit
        return ""

    def sync_account(
        self,
        account: Any,
        *,
        mailboxes: Iterable[str] | None = None,
        limit: int = 200,
        full: bool = False,
        on_progress: Any = None,
    ) -> list[SyncStats]:
        """
        收取单个账户。每个目录一条 SyncStats。
        单个目录失败只记进该目录的 error，不影响其余目录。
        """
        acc_name = getattr(account, "name", "") or ""
        parent_mail = (getattr(account, "mail", "") or acc_name).strip()

        client = mail_client_from_account(account, timeout=self.timeout)
        cursor_sync = bool(getattr(client, "supports_sync_cursor", False))
        folders = (
            [str(f).strip() for f in mailboxes if str(f).strip()]
            if cursor_sync and mailboxes
            else list(DEFAULT_SYNC_FOLDERS)
            if cursor_sync
            else resolve_sync_folders(client, mailboxes)
        )
        alias_index = self._alias_index(acc_name, parent_mail)

        results: list[SyncStats] = []
        for box in folders:
            stats = SyncStats(account=acc_name, mailbox=box)
            box_label = mailbox_label(box)
            if callable(on_progress):
                on_progress(f"{acc_name} / {box_label} 开始收取")
            try:
                state = self.db.get_sync_state(acc_name, box)
                if cursor_sync:
                    cursor = "" if full else str(state.get("sync_cursor") or "")
                    items, next_cursor = client.fetch_since_cursor(
                        mailbox=box,
                        cursor=cursor,
                        limit=limit,
                        include_body=True,
                        body_limit=SYNC_BODY_LIMIT,
                    )
                    max_uid = int(state.get("last_uid") or 0)
                else:
                    since = 0 if full else int(state.get("last_uid") or 0)
                    items, max_uid = client.fetch_since_uid(
                        mailbox=box,
                        since_uid=since,
                        limit=limit,
                        include_body=True,
                        body_limit=SYNC_BODY_LIMIT,
                    )
                    next_cursor = None
                stats.fetched = len(items)

                for item in items:
                    uid = str(item.get("uid") or "")
                    if not uid:
                        continue
                    alias_hme = self._match_alias(item, alias_index)
                    if alias_hme:
                        stats.matched_alias += 1
                    flags = list(item.get("flags") or [])
                    _, is_new = self.db.upsert_mail(
                        account=acc_name,
                        parent_mail=parent_mail,
                        mailbox=box,
                        uid=uid,
                        message_id=item.get("message_id") or "",
                        alias_hme=alias_hme,
                        from_name=item.get("from_name") or "",
                        from_addr=item.get("from_addr") or "",
                        sender_addr=item.get("sender_addr") or "",
                        return_path=item.get("return_path_addr") or "",
                        received_spf=item.get("received_spf") or "",
                        envelope_from=item.get("envelope_from") or "",
                        is_relayed=bool(item.get("is_relayed")),
                        relay_label=item.get("relay_label") or "",
                        to_addr=item.get("to") or "",
                        delivered_to=item.get("delivered_to") or "",
                        subject=item.get("subject") or "",
                        mail_type=item.get("type") or "other",
                        code=item.get("code") or "",
                        summary=item.get("summary") or "",
                        date_header=item.get("date") or "",
                        date_utc=item.get("date_parsed") or "",
                        internaldate=item.get("internaldate") or "",
                        size=item.get("size"),
                        flags=flags,
                        is_seen=any("seen" in str(f).lower() for f in flags),
                        attachments=item.get("attachments") or [],
                        body_text_len=int(item.get("body_text_len") or 0),
                        body_html_len=int(item.get("body_html_len") or 0),
                        content_type=item.get("content_type") or "",
                    )
                    if is_new:
                        stats.saved += 1
                    else:
                        stats.updated += 1
                    mtype = item.get("type") or "other"
                    stats.by_type[mtype] = stats.by_type.get(mtype, 0) + 1

                stats.last_uid = max_uid
                self.db.set_sync_state(
                    acc_name,
                    box,
                    last_uid=max_uid,
                    sync_cursor=next_cursor,
                    status="ok",
                    saved_delta=stats.saved,
                )
            except Exception as e:
                stats.ok = False
                stats.error = f"{type(e).__name__}: {e}"
                self.db.set_sync_state(
                    acc_name,
                    box,
                    last_uid=int(self.db.get_sync_state(acc_name, box).get("last_uid") or 0),
                    status="error",
                    error=stats.error,
                )
            if callable(on_progress):
                on_progress(
                    f"{acc_name} / {box_label} "
                    + (
                        f"完成 新增{stats.saved} 更新{stats.updated}"
                        if stats.ok
                        else f"失败 {stats.error}"
                    )
                )
            results.append(stats)
        return results

    def sync_accounts(
        self,
        accounts: Iterable[Any],
        *,
        mailboxes: Iterable[str] | None = None,
        limit: int = 200,
        full: bool = False,
        skip_unready: bool = False,
        on_progress: Any = None,
    ) -> list[SyncStats]:
        """收取多个账户；某账户整体失败（如凭证不全）也不中断其余账户。"""
        out: list[SyncStats] = []
        for acc in accounts:
            name = getattr(acc, "name", "") or "?"
            if not getattr(acc, "mail_ready", False):
                inbox = (getattr(acc, "inbox_mail", "") or "").strip()
                reason = (
                    f"收件 provider 未配置：inbox={inbox}，需要对应邮箱服务商的收件授权"
                    if inbox
                    else "收件 provider 未配置：需要 app_password 或 inbox provider 配置"
                )
                if callable(on_progress):
                    on_progress(f"{name} / 收件 {'跳过' if skip_unready else '失败'}：{reason}")
                out.append(
                    SyncStats(
                        account=name,
                        mailbox="-",
                        ok=bool(skip_unready),
                        error="" if skip_unready else reason,
                        skipped=bool(skip_unready),
                        note=reason if skip_unready else "",
                    )
                )
                continue
            try:
                out.extend(
                    self.sync_account(
                        acc,
                        mailboxes=mailboxes,
                        limit=limit,
                        full=full,
                        on_progress=on_progress,
                    )
                )
            except Exception as e:
                out.append(
                    SyncStats(
                        account=name,
                        mailbox="-",
                        ok=False,
                        error=f"{type(e).__name__}: {e}",
                    )
                )
        return out


def default_db_path(settings: Any) -> Path:
    return Path(settings.base_dir) / "db" / "aliases.db"


def sync_service_from_settings(settings: Any) -> MailSyncService:
    return MailSyncService(AliasDB(default_db_path(settings)))
