from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .icloud_plan import ICloudFreePlanError, ICloudPlan
from .rate_limit import (
    HME_ACCOUNT_ALIAS_LIMIT,
    HME_CREATE_LIMIT_PER_HOUR,
    HME_CREATE_MAX_INTERVAL_SECONDS,
    HME_CREATE_MIN_INTERVAL_SECONDS,
    HME_CREATE_WINDOW_SECONDS,
    HMEAccountAliasLimitError,
    HMECreateRateLimitError,
    pick_create_interval_seconds,
)

# CDK_ + 8~64 hex
CDK_RE = re.compile(r"^CDK_[0-9a-fA-F]{8,64}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


def unix_now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def to_unix(ts: str | int | float | None) -> int:
    if ts is None or ts == "":
        return 0
    if isinstance(ts, (int, float)):
        return int(ts)
    parsed = parse_utc(str(ts))
    return int(parsed.timestamp()) if parsed else 0


def parse_utc(ts: str) -> datetime | None:
    if not ts:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def normalize_cdk(value: str | None, *, allow_bare_hex: bool = True) -> str:
    """
    规范化 CDK 字符串；非法则返回空串。
    标准形式：CDK_ + 8~64 位 hex（小写）。
    allow_bare_hex=True 时，查询可只传 hex 体。
    """
    if not value:
        return ""
    s = str(value).strip()
    if not s:
        return ""

    # 兼容旧前缀 cdk / CDK（无下划线）: cdk + hex
    m_old = re.fullmatch(r"(?i)cdk([0-9a-f]{8,64})", s)
    if m_old:
        return "CDK_" + m_old.group(1).lower()

    if s.upper().startswith("CDK_"):
        body = s[4:]
        if re.fullmatch(r"[0-9a-fA-F]{8,64}", body):
            return "CDK_" + body.lower()
        return ""

    # 查询时允许裸 hex；从 label 提取时不要把 AAAA... 误判为 CDK
    if allow_bare_hex and re.fullmatch(r"[0-9a-fA-F]{8,64}", s):
        return "CDK_" + s.lower()
    return ""


def extract_cdk_from_label(label: str | None) -> str:
    """仅从明确 CDK 形态的 label 提取（避免纯 A/hex 测试标签误识别）。"""
    if not label:
        return ""
    s = str(label).strip()
    if s.upper().startswith("CDK_") or re.fullmatch(r"(?i)cdk[0-9a-f]{8,64}", s):
        return normalize_cdk(s, allow_bare_hex=False)
    return ""


@dataclass
class AliasRecord:
    id: int
    cdk: str
    parent_mail: str
    account: str
    hme: str
    label: str
    anonymous_id: str
    is_active: int
    create_timestamp: int | None
    note: str
    source: str
    raw_json: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["is_active"] = bool(self.is_active)
        # 业务键值对：CDK -> 隐私邮箱 + 母号
        d["mapping"] = {
            "cdk": self.cdk,
            "hme": self.hme,
            "parent_mail": self.parent_mail or self.account,
        }
        return d


@dataclass
class MailRecord:
    """一封邮件的元数据 + 分类结果（不含正文）。"""

    id: int
    account: str
    parent_mail: str
    mailbox: str
    uid: str
    message_id: str
    alias_hme: str
    from_name: str
    from_addr: str
    sender_addr: str
    return_path: str
    received_spf: str
    envelope_from: str
    is_relayed: int
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
    size: int | None
    flags_json: str
    is_seen: int
    has_attachment: int
    attachments_json: str
    body_text_len: int
    body_html_len: int
    content_type: str
    fetched_at: str
    created_at: str
    updated_at: str
    body_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["is_relayed"] = bool(self.is_relayed)
        d["is_seen"] = bool(self.is_seen)
        d["has_attachment"] = bool(self.has_attachment)
        d["flags"] = _loads_list(self.flags_json)
        d["attachments"] = _loads_list(self.attachments_json)
        d.pop("flags_json", None)
        d.pop("attachments_json", None)
        return d


def _loads_list(raw: str) -> list[Any]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


@dataclass
class CreateQuota:
    account: str
    used: int
    limit: int
    window_seconds: int
    remaining: int
    retry_after_sec: int
    recent: list[dict[str, Any]]
    last_produce_at: int = 0
    next_produce_at: int = 0
    interval_min_sec: int = HME_CREATE_MIN_INTERVAL_SECONDS
    interval_max_sec: int = HME_CREATE_MAX_INTERVAL_SECONDS

    @property
    def allowed(self) -> bool:
        return self.remaining > 0


@dataclass
class AliasCapacity:
    account: str
    alias_count: int
    pending: int
    limit: int
    remaining: int

    @property
    def allowed(self) -> bool:
        return self.remaining > 0


class AliasDB:
    """
    别名库：以 CDK 定位 隐私邮箱(hme) + 母号(parent_mail)。

    键值关系：
      CDK_xxx  ->  { parent_mail, hme, anonymous_id, is_active, ... }
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # check_same_thread=False：每次调用都新建独立连接，不跨线程复用，
        # 但 web 端同步路由跑在线程池里，需要放开该检查。
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL 让「收信写入」与「页面读取」不互相阻塞（持久属性，重复设置无害）
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:
            pass
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _table_columns(self, conn: sqlite3.Connection, table: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r["name"] for r in rows}

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cdk TEXT,
                    parent_mail TEXT NOT NULL DEFAULT '',
                    account TEXT NOT NULL,
                    hme TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    anonymous_id TEXT NOT NULL DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    create_timestamp INTEGER,
                    note TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(account, hme)
                )
                """
            )
            # 迁移旧库：补列
            cols = self._table_columns(conn, "aliases")
            if "cdk" not in cols:
                conn.execute("ALTER TABLE aliases ADD COLUMN cdk TEXT")
            if "parent_mail" not in cols:
                conn.execute(
                    "ALTER TABLE aliases ADD COLUMN parent_mail TEXT NOT NULL DEFAULT ''"
                )

            # 回填 parent_mail / cdk
            conn.execute(
                """
                UPDATE aliases
                SET parent_mail = account
                WHERE parent_mail IS NULL OR parent_mail = ''
                """
            )
            rows = conn.execute(
                "SELECT id, label, cdk FROM aliases"
            ).fetchall()
            for r in rows:
                raw_cdk = r["cdk"] or ""
                # 纠正历史误写入：纯 A/hex 标签不应成为 CDK
                cdk = normalize_cdk(raw_cdk, allow_bare_hex=False) if raw_cdk.upper().startswith("CDK_") or re.fullmatch(r"(?i)cdk[0-9a-f]{8,64}", raw_cdk or "") else ""
                if not cdk:
                    cdk = extract_cdk_from_label(r["label"])
                if not cdk:
                    if raw_cdk:
                        conn.execute(
                            "UPDATE aliases SET cdk = NULL WHERE id = ?",
                            (r["id"],),
                        )
                    continue
                if cdk != raw_cdk:
                    conn.execute(
                        "UPDATE aliases SET cdk = ? WHERE id = ?",
                        (cdk, r["id"]),
                    )

            # 清空仍非法的 cdk
            bad = conn.execute(
                "SELECT id, cdk FROM aliases WHERE cdk IS NOT NULL AND cdk != ''"
            ).fetchall()
            for r in bad:
                if not CDK_RE.match(r["cdk"] or ""):
                    conn.execute(
                        "UPDATE aliases SET cdk = NULL WHERE id = ?",
                        (r["id"],),
                    )

            # 处理重复 CDK：保留最小 id，其余清空
            dups = conn.execute(
                """
                SELECT cdk FROM aliases
                WHERE cdk IS NOT NULL AND cdk != ''
                GROUP BY cdk HAVING COUNT(*) > 1
                """
            ).fetchall()
            for d in dups:
                ids = [
                    x["id"]
                    for x in conn.execute(
                        "SELECT id FROM aliases WHERE cdk = ? ORDER BY id ASC",
                        (d["cdk"],),
                    ).fetchall()
                ]
                for rid in ids[1:]:
                    conn.execute(
                        "UPDATE aliases SET cdk = NULL WHERE id = ?",
                        (rid,),
                    )

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_aliases_account ON aliases(account)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_aliases_parent_mail ON aliases(parent_mail)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_aliases_hme ON aliases(hme)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_aliases_anonymous_id ON aliases(anonymous_id)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_aliases_cdk_unique "
                "ON aliases(cdk) WHERE cdk IS NOT NULL AND cdk != ''"
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS create_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account TEXT NOT NULL,
                    parent_mail TEXT NOT NULL DEFAULT '',
                    hme TEXT NOT NULL DEFAULT '',
                    cdk TEXT NOT NULL DEFAULT '',
                    label TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    produce_at INTEGER NOT NULL DEFAULT 0,
                    next_produce_at INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS create_claims (
                    claim_id TEXT PRIMARY KEY,
                    account TEXT NOT NULL,
                    claimed_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_create_claims_account_time ON create_claims(account, claimed_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS production_jobs (
                    job_id TEXT PRIMARY KEY,
                    account TEXT NOT NULL,
                    interface TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested INTEGER NOT NULL DEFAULT 0,
                    created INTEGER NOT NULL DEFAULT 0,
                    progress_json TEXT NOT NULL DEFAULT '[]',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_production_jobs_updated ON production_jobs(updated_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS production_loop_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'stopped',
                    interface TEXT NOT NULL DEFAULT 'legacy',
                    mode TEXT NOT NULL DEFAULT 'forever',
                    duration_minutes INTEGER NOT NULL DEFAULT 0,
                    interval_sec INTEGER NOT NULL DEFAULT 2,
                    selected_accounts_json TEXT NOT NULL DEFAULT '[]',
                    started_at TEXT NOT NULL DEFAULT '',
                    deadline_at INTEGER NOT NULL DEFAULT 0,
                    current_account TEXT NOT NULL DEFAULT '',
                    current_job_id TEXT NOT NULL DEFAULT '',
                    last_account TEXT NOT NULL DEFAULT '',
                    last_job_id TEXT NOT NULL DEFAULT '',
                    next_run_at INTEGER NOT NULL DEFAULT 0,
                    round_no INTEGER NOT NULL DEFAULT 0,
                    submitted INTEGER NOT NULL DEFAULT 0,
                    created INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    progress_json TEXT NOT NULL DEFAULT '[]',
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO production_loop_state (id, updated_at)
                VALUES (1, ?)
                """,
                (utc_now(),),
            )
            loop_cols = self._table_columns(conn, "production_loop_state")
            if "last_account" not in loop_cols:
                conn.execute(
                    "ALTER TABLE production_loop_state ADD COLUMN last_account TEXT NOT NULL DEFAULT ''"
                )
            if "last_job_id" not in loop_cols:
                conn.execute(
                    "ALTER TABLE production_loop_state ADD COLUMN last_job_id TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS client_sync_state (
                    client_id TEXT PRIMARY KEY,
                    last_seen_at TEXT NOT NULL,
                    user_agent TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS account_flags (
                    account TEXT PRIMARY KEY,
                    cookie_invalid INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    marked_at INTEGER NOT NULL DEFAULT 0,
                    cleared_at INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            flag_cols = self._table_columns(conn, "account_flags")
            for column, declaration in (
                ("free_plan", "INTEGER NOT NULL DEFAULT 0"),
                ("plan_name", "TEXT NOT NULL DEFAULT ''"),
                ("plan_checked_at", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in flag_cols:
                    conn.execute(f"ALTER TABLE account_flags ADD COLUMN {column} {declaration}")
            ev_cols = self._table_columns(conn, "create_events")
            if "cdk" not in ev_cols:
                conn.execute(
                    "ALTER TABLE create_events ADD COLUMN cdk TEXT NOT NULL DEFAULT ''"
                )
            if "parent_mail" not in ev_cols:
                conn.execute(
                    "ALTER TABLE create_events ADD COLUMN parent_mail TEXT NOT NULL DEFAULT ''"
                )
            if "produce_at" not in ev_cols:
                conn.execute(
                    "ALTER TABLE create_events ADD COLUMN produce_at INTEGER NOT NULL DEFAULT 0"
                )
            if "next_produce_at" not in ev_cols:
                conn.execute(
                    "ALTER TABLE create_events ADD COLUMN next_produce_at INTEGER NOT NULL DEFAULT 0"
                )
            # 旧行只有 created_at 文本时间：回填 unix，并把下次可生产时间垫到 13 分钟后
            conn.execute(
                """
                UPDATE create_events
                SET produce_at = CAST(strftime('%s', created_at) AS INTEGER)
                WHERE produce_at = 0 AND created_at IS NOT NULL AND created_at != ''
                """
            )
            conn.execute(
                """
                UPDATE create_events
                SET next_produce_at = produce_at + ?
                WHERE next_produce_at = 0 AND produce_at > 0
                """,
                (HME_CREATE_MIN_INTERVAL_SECONDS,),
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_create_events_account_time "
                "ON create_events(account, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_create_events_cdk "
                "ON create_events(cdk)"
            )

            # ---------- 邮件（只存元数据 + 分类结果，正文按需实时拉取）----------
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mails (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account TEXT NOT NULL,
                    parent_mail TEXT NOT NULL DEFAULT '',
                    mailbox TEXT NOT NULL DEFAULT 'INBOX',
                    uid TEXT NOT NULL,
                    message_id TEXT NOT NULL DEFAULT '',
                    alias_hme TEXT NOT NULL DEFAULT '',
                    from_name TEXT NOT NULL DEFAULT '',
                    from_addr TEXT NOT NULL DEFAULT '',
                    sender_addr TEXT NOT NULL DEFAULT '',
                    return_path TEXT NOT NULL DEFAULT '',
                    received_spf TEXT NOT NULL DEFAULT '',
                    is_relayed INTEGER NOT NULL DEFAULT 0,
                    relay_label TEXT NOT NULL DEFAULT '',
                    to_addr TEXT NOT NULL DEFAULT '',
                    delivered_to TEXT NOT NULL DEFAULT '',
                    subject TEXT NOT NULL DEFAULT '',
                    mail_type TEXT NOT NULL DEFAULT 'other',
                    code TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    date_header TEXT NOT NULL DEFAULT '',
                    date_utc TEXT NOT NULL DEFAULT '',
                    internaldate TEXT NOT NULL DEFAULT '',
                    size INTEGER,
                    flags_json TEXT NOT NULL DEFAULT '',
                    is_seen INTEGER NOT NULL DEFAULT 0,
                    has_attachment INTEGER NOT NULL DEFAULT 0,
                    attachments_json TEXT NOT NULL DEFAULT '',
                    body_text_len INTEGER NOT NULL DEFAULT 0,
                    body_html_len INTEGER NOT NULL DEFAULT 0,
                    content_type TEXT NOT NULL DEFAULT '',
                    fetched_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_mails_uid_unique "
                "ON mails(account, mailbox, uid)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mails_account_date "
                "ON mails(account, date_utc DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mails_type ON mails(mail_type)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mails_alias ON mails(alias_hme)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mails_from ON mails(from_addr)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mails_message_id ON mails(message_id)"
            )
            mail_cols = self._table_columns(conn, "mails")
            if "envelope_from" not in mail_cols:
                conn.execute(
                    "ALTER TABLE mails ADD COLUMN envelope_from TEXT NOT NULL DEFAULT ''"
                )
            if "received_spf" not in mail_cols:
                conn.execute(
                    "ALTER TABLE mails ADD COLUMN received_spf TEXT NOT NULL DEFAULT ''"
                )
            if "body_text" not in mail_cols:
                conn.execute(
                    "ALTER TABLE mails ADD COLUMN body_text TEXT NOT NULL DEFAULT ''"
                )

            # ---------- 增量收取水位 ----------
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mail_sync_state (
                    account TEXT NOT NULL,
                    mailbox TEXT NOT NULL,
                    last_uid INTEGER NOT NULL DEFAULT 0,
                    sync_cursor TEXT NOT NULL DEFAULT '',
                    last_sync_at TEXT NOT NULL DEFAULT '',
                    last_status TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    total_saved INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (account, mailbox)
                )
                """
            )
            sync_cols = self._table_columns(conn, "mail_sync_state")
            if "sync_cursor" not in sync_cols:
                conn.execute(
                    "ALTER TABLE mail_sync_state ADD COLUMN sync_cursor TEXT NOT NULL DEFAULT ''"
                )

            # ---------- 分组（本期只建表 + 最小 CRUD，UI 后续再做）----------
            # 用独立关联表而非给 aliases 加列：upsert_alias 在同步时会覆写别名行，
            # 分组信息挂在同表上有被同步逻辑冲掉的风险；关联表也支持一别名多组。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alias_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    account TEXT NOT NULL DEFAULT '',
                    color TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_alias_groups_name "
                "ON alias_groups(account, name)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alias_group_members (
                    group_id INTEGER NOT NULL,
                    hme TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, hme)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_alias_group_members_hme "
                "ON alias_group_members(hme)"
            )

            # ---------- 本地领取订单（无真实交易；领走即占用）----------
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS claim_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_no TEXT NOT NULL UNIQUE,
                    contact_email TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'claimed',
                    deliver_status TEXT NOT NULL DEFAULT 'pending',
                    deliver_error TEXT NOT NULL DEFAULT '',
                    deliver_from TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_claim_orders_created "
                "ON claim_orders(created_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_claim_orders_contact "
                "ON claim_orders(contact_email)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS claim_order_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL,
                    order_no TEXT NOT NULL,
                    hme TEXT NOT NULL UNIQUE,
                    account TEXT NOT NULL DEFAULT '',
                    parent_mail TEXT NOT NULL DEFAULT '',
                    label TEXT NOT NULL DEFAULT '',
                    cdk TEXT NOT NULL DEFAULT '',
                    access_token TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(order_id) REFERENCES claim_orders(id)
                )
                """
            )
            item_cols = self._table_columns(conn, "claim_order_items")
            if "access_token" not in item_cols:
                conn.execute(
                    "ALTER TABLE claim_order_items ADD COLUMN access_token TEXT NOT NULL DEFAULT ''"
                )
            # 旧订单补发独立 token，方便导出 邮箱----取码地址
            missing_token_rows = conn.execute(
                """
                SELECT id FROM claim_order_items
                WHERE access_token IS NULL OR trim(access_token) = ''
                """
            ).fetchall()
            for row in missing_token_rows:
                conn.execute(
                    "UPDATE claim_order_items SET access_token = ? WHERE id = ?",
                    (uuid.uuid4().hex, row["id"]),
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_claim_order_items_order "
                "ON claim_order_items(order_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_claim_order_items_hme "
                "ON claim_order_items(hme)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_claim_order_items_token "
                "ON claim_order_items(access_token) "
                "WHERE access_token IS NOT NULL AND access_token != ''"
            )
            conn.commit()

    def upsert_alias(
        self,
        *,
        account: str,
        hme: str,
        label: str = "",
        anonymous_id: str = "",
        is_active: bool = True,
        create_timestamp: int | None = None,
        note: str = "",
        source: str = "",
        raw: Any = None,
        parent_mail: str | None = None,
        cdk: str | None = None,
        preserve_created_at: bool = True,
    ) -> None:
        now = utc_now()
        parent = (parent_mail or account or "").strip()
        if cdk is not None and str(cdk).strip():
            cdk_val = normalize_cdk(cdk, allow_bare_hex=False) or extract_cdk_from_label(str(cdk))
        else:
            cdk_val = extract_cdk_from_label(label)
        raw_json = json.dumps(raw, ensure_ascii=False, default=str) if raw is not None else ""

        with self._connect() as conn:
            if not cdk_val:
                cdk_val = extract_cdk_from_label(label)

            conn.execute(
                """
                INSERT INTO aliases (
                    cdk, parent_mail, account, hme, label, anonymous_id, is_active,
                    create_timestamp, note, source, raw_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account, hme) DO UPDATE SET
                    cdk=CASE
                        WHEN excluded.cdk IS NOT NULL AND excluded.cdk != '' THEN excluded.cdk
                        ELSE aliases.cdk
                    END,
                    parent_mail=CASE
                        WHEN excluded.parent_mail != '' THEN excluded.parent_mail
                        ELSE aliases.parent_mail
                    END,
                    label=CASE
                        WHEN excluded.label != '' THEN excluded.label
                        ELSE aliases.label
                    END,
                    anonymous_id=CASE
                        WHEN excluded.anonymous_id != '' THEN excluded.anonymous_id
                        ELSE aliases.anonymous_id
                    END,
                    is_active=excluded.is_active,
                    create_timestamp=COALESCE(excluded.create_timestamp, aliases.create_timestamp),
                    note=CASE WHEN excluded.note != '' THEN excluded.note ELSE aliases.note END,
                    source=CASE WHEN excluded.source != '' THEN excluded.source ELSE aliases.source END,
                    raw_json=CASE WHEN excluded.raw_json != '' THEN excluded.raw_json ELSE aliases.raw_json END,
                    updated_at=excluded.updated_at
                """,
                (
                    cdk_val or None,
                    parent,
                    account,
                    hme,
                    label,
                    anonymous_id,
                    1 if is_active else 0,
                    create_timestamp,
                    note,
                    source,
                    raw_json,
                    now,
                    now,
                ),
            )
            conn.commit()

    def record_create_event(
        self,
        account: str,
        hme: str = "",
        label: str = "",
        cdk: str = "",
        parent_mail: str = "",
    ) -> str:
        """记录一次成功创建（用于 1 小时限流）。返回 created_at。"""
        now = utc_now()
        produce_at = unix_now()
        next_produce_at = produce_at + pick_create_interval_seconds()
        cdk_val = normalize_cdk(cdk) or extract_cdk_from_label(label)
        parent = (parent_mail or account or "").strip()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO create_events (
                    account, parent_mail, hme, cdk, label, created_at,
                    produce_at, next_produce_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (account, parent, hme, cdk_val, label, now, produce_at, next_produce_at),
            )
            conn.commit()
        return now

    def claim_create_slot(
        self,
        account: str,
        *,
        limit: int = HME_CREATE_LIMIT_PER_HOUR,
        window_seconds: int = HME_CREATE_WINDOW_SECONDS,
    ) -> str:
        """原子占用一个创建名额，防止并发客户端越过每小时限制。"""
        account = (account or "").strip()
        if not account:
            raise ValueError("account 不能为空")
        now = utc_now_dt()
        now_unix = int(now.timestamp())
        since_s = (now - timedelta(seconds=window_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        claim_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            flag = conn.execute(
                "SELECT free_plan FROM account_flags WHERE account = ?", (account,)
            ).fetchone()
            if flag and flag["free_plan"]:
                raise ICloudFreePlanError(account)
            conn.execute("DELETE FROM create_claims WHERE claimed_at < ?", (since_s,))
            last = conn.execute(
                """
                SELECT produce_at, next_produce_at, created_at
                FROM create_events
                WHERE account = ?
                ORDER BY COALESCE(NULLIF(produce_at, 0), CAST(strftime('%s', created_at) AS INTEGER)) DESC
                LIMIT 1
                """,
                (account,),
            ).fetchone()
            events = conn.execute(
                "SELECT created_at FROM create_events WHERE account = ? AND created_at >= ? ORDER BY created_at ASC",
                (account, since_s),
            ).fetchall()
            active = conn.execute(
                "SELECT claim_id, claimed_at FROM create_claims WHERE account = ? AND claimed_at >= ?",
                (account, since_s),
            ).fetchall()
            alias_row = conn.execute(
                "SELECT COUNT(*) AS total FROM aliases WHERE account = ? OR parent_mail = ?",
                (account, account),
            ).fetchone()
            alias_count = int(alias_row["total"] or 0)
            if alias_count + len(active) >= HME_ACCOUNT_ALIAS_LIMIT:
                raise HMEAccountAliasLimitError(
                    account,
                    alias_count,
                    HME_ACCOUNT_ALIAS_LIMIT,
                    pending=len(active),
                )
            used = len(events) + len(active)
            next_at = 0
            if last:
                next_at = int(last["next_produce_at"] or 0)
                if next_at <= 0:
                    last_at = int(last["produce_at"] or 0) or to_unix(last["created_at"])
                    if last_at:
                        next_at = last_at + HME_CREATE_MIN_INTERVAL_SECONDS
            if active and next_at <= now_unix:
                newest_claim = max(to_unix(row["claimed_at"]) for row in active)
                next_at = max(next_at, newest_claim + HME_CREATE_MIN_INTERVAL_SECONDS)
            if next_at > now_unix:
                raise HMECreateRateLimitError(
                    account,
                    used,
                    limit,
                    next_at - now_unix,
                    reason="interval",
                    next_produce_at=next_at,
                )
            if used >= int(limit):
                retry_after = 0
                if events:
                    oldest = parse_utc(events[0]["created_at"])
                    if oldest:
                        retry_after = max(
                            0, int((oldest + timedelta(seconds=window_seconds) - now).total_seconds())
                        )
                raise HMECreateRateLimitError(account, used, limit, retry_after)
            conn.execute(
                "INSERT INTO create_claims (claim_id, account, claimed_at) VALUES (?, ?, ?)",
                (claim_id, account, utc_now()),
            )
            conn.commit()
        return claim_id

    def release_create_claim(self, claim_id: str) -> None:
        if not claim_id:
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM create_claims WHERE claim_id = ?", (claim_id,))
            conn.commit()

    def release_all_create_claims(self) -> int:
        """服务启动时清理上个进程遗留的临时并发占位。"""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM create_claims")
            conn.commit()
        return max(0, int(cursor.rowcount or 0))

    def get_create_quota(
        self,
        account: str,
        limit: int = HME_CREATE_LIMIT_PER_HOUR,
        window_seconds: int = HME_CREATE_WINDOW_SECONDS,
    ) -> CreateQuota:
        now = utc_now_dt()
        now_unix = int(now.timestamp())
        since = now - timedelta(seconds=window_seconds)
        since_s = since.strftime("%Y-%m-%d %H:%M:%S")

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT hme, label, cdk, created_at, produce_at, next_produce_at
                FROM create_events
                WHERE account = ? AND created_at >= ?
                ORDER BY created_at ASC
                """,
                (account, since_s),
            ).fetchall()
            last = conn.execute(
                """
                SELECT produce_at, next_produce_at, created_at
                FROM create_events
                WHERE account = ?
                ORDER BY COALESCE(NULLIF(produce_at, 0), CAST(strftime('%s', created_at) AS INTEGER)) DESC
                LIMIT 1
                """,
                (account,),
            ).fetchone()
            active_row = conn.execute(
                "SELECT COUNT(*) AS n FROM create_claims WHERE account = ? AND claimed_at >= ?",
                (account, since_s),
            ).fetchone()

        recent = [
            {
                "hme": r["hme"],
                "label": r["label"],
                "cdk": r["cdk"] if "cdk" in r.keys() else "",
                "created_at": r["created_at"],
                "produce_at": int(r["produce_at"] or 0) if "produce_at" in r.keys() else 0,
                "next_produce_at": int(r["next_produce_at"] or 0) if "next_produce_at" in r.keys() else 0,
            }
            for r in rows
        ]
        last_produce_at = 0
        next_produce_at = 0
        if last:
            last_produce_at = int(last["produce_at"] or 0) or to_unix(last["created_at"])
            next_produce_at = int(last["next_produce_at"] or 0)
            if next_produce_at <= 0 and last_produce_at:
                next_produce_at = last_produce_at + HME_CREATE_MIN_INTERVAL_SECONDS
        active_claims = int(active_row["n"] if active_row else 0)
        used = len(recent) + active_claims
        remaining = max(0, limit - used)
        retry_after = 0
        if used >= limit and recent:
            oldest = parse_utc(recent[0]["created_at"])
            if oldest:
                unlock_at = oldest + timedelta(seconds=window_seconds)
                retry_after = max(0, int((unlock_at - now).total_seconds()))
        if next_produce_at > now_unix:
            remaining = 0
            retry_after = max(retry_after, next_produce_at - now_unix)

        return CreateQuota(
            account=account,
            used=used,
            limit=limit,
            window_seconds=window_seconds,
            remaining=remaining,
            retry_after_sec=retry_after,
            recent=recent,
            last_produce_at=last_produce_at,
            next_produce_at=next_produce_at,
        )

    # ---------- 生产任务 / 多客户端同步 ----------

    def create_production_job(
        self, job_id: str, account: str, interface: str, requested: int
    ) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO production_jobs (
                    job_id, account, interface, status, requested, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (job_id, account, interface, max(0, int(requested)), now),
            )
            conn.commit()

    def update_production_job(self, job_id: str, **changes: Any) -> None:
        allowed = {
            "status", "created", "progress_json", "result_json", "error",
            "started_at", "finished_at",
        }
        fields = [(key, value) for key, value in changes.items() if key in allowed]
        if not fields:
            return
        fields.append(("updated_at", utc_now()))
        sql = "UPDATE production_jobs SET " + ", ".join(f"{key} = ?" for key, _ in fields)
        with self._connect() as conn:
            conn.execute(sql + " WHERE job_id = ?", [value for _, value in fields] + [job_id])
            conn.commit()

    @staticmethod
    def _production_job_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        try:
            result["progress"] = json.loads(result.pop("progress_json") or "[]")
        except (TypeError, ValueError):
            result["progress"] = []
        try:
            result["result"] = json.loads(result.pop("result_json") or "{}")
        except (TypeError, ValueError):
            result["result"] = {}
        return result

    def get_production_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM production_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._production_job_dict(row) if row else None

    def list_production_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM production_jobs ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [self._production_job_dict(row) for row in rows]

    def get_production_loop_state(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM production_loop_state WHERE id = 1"
            ).fetchone()
        if not row:
            return {}
        state = dict(row)
        state["enabled"] = bool(state.get("enabled"))
        for source, target in (
            ("selected_accounts_json", "selected_accounts"),
            ("progress_json", "progress"),
        ):
            try:
                value = json.loads(state.pop(source) or "[]")
            except (TypeError, ValueError):
                value = []
            state[target] = value if isinstance(value, list) else []
        return state

    def update_production_loop_state(self, **changes: Any) -> dict[str, Any]:
        allowed = {
            "enabled",
            "status",
            "interface",
            "mode",
            "duration_minutes",
            "interval_sec",
            "selected_accounts_json",
            "started_at",
            "deadline_at",
            "current_account",
            "current_job_id",
            "last_account",
            "last_job_id",
            "next_run_at",
            "round_no",
            "submitted",
            "created",
            "failed",
            "skipped",
            "progress_json",
            "last_error",
        }
        normalized: dict[str, Any] = {}
        for key, value in changes.items():
            target = key
            if key == "selected_accounts":
                target = "selected_accounts_json"
                value = json.dumps(list(value or []), ensure_ascii=False)
            elif key == "progress":
                target = "progress_json"
                value = json.dumps(list(value or [])[-200:], ensure_ascii=False)
            elif key == "enabled":
                value = 1 if value else 0
            if target in allowed:
                normalized[target] = value
        if not normalized:
            return self.get_production_loop_state()
        normalized["updated_at"] = utc_now()
        sql = "UPDATE production_loop_state SET " + ", ".join(
            f"{key} = ?" for key in normalized
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if "selected_accounts_json" in normalized:
                blocked = {row["account"] for row in conn.execute(
                    "SELECT account FROM account_flags WHERE free_plan = 1"
                )}
                selected = json.loads(normalized["selected_accounts_json"])
                normalized["selected_accounts_json"] = json.dumps(
                    [name for name in selected if name not in blocked], ensure_ascii=False
                )
            conn.execute(
                sql + " WHERE id = 1",
                [*normalized.values()],
            )
            conn.commit()
        return self.get_production_loop_state()

    def mark_incomplete_production_jobs(self, reason: str = "服务重启，任务已中断") -> int:
        now = utc_now()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE production_jobs
                SET status = 'error', error = ?, finished_at = ?, updated_at = ?
                WHERE status IN ('pending', 'running')
                """,
                ((reason or "服务重启，任务已中断")[:500], now, now),
            )
            conn.commit()
            return int(cur.rowcount or 0)

    def mark_cookie_invalid(self, account: str, reason: str = "http_421") -> dict[str, Any]:
        account = (account or "").strip()
        if not account:
            raise ValueError("account 不能为空")
        now = utc_now()
        now_unix = unix_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO account_flags (account, cookie_invalid, reason, marked_at, cleared_at, updated_at)
                VALUES (?, 1, ?, ?, 0, ?)
                ON CONFLICT(account) DO UPDATE SET
                    cookie_invalid=1,
                    reason=excluded.reason,
                    marked_at=excluded.marked_at,
                    updated_at=excluded.updated_at
                """,
                (account, (reason or "http_421")[:200], now_unix, now),
            )
            conn.commit()
        return self.get_account_flag(account) or {
            "account": account,
            "cookie_invalid": True,
            "reason": reason,
            "marked_at": now_unix,
            "cleared_at": 0,
        }

    def clear_cookie_invalid(self, account: str) -> dict[str, Any]:
        account = (account or "").strip()
        if not account:
            raise ValueError("account 不能为空")
        now = utc_now()
        now_unix = unix_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO account_flags (account, cookie_invalid, reason, marked_at, cleared_at, updated_at)
                VALUES (?, 0, '', 0, ?, ?)
                ON CONFLICT(account) DO UPDATE SET
                    cookie_invalid=0,
                    reason='',
                    cleared_at=excluded.cleared_at,
                    updated_at=excluded.updated_at
                """,
                (account, now_unix, now),
            )
            conn.commit()
        return self.get_account_flag(account) or {
            "account": account,
            "cookie_invalid": False,
            "reason": "",
            "marked_at": 0,
            "cleared_at": now_unix,
        }

    def save_account_plan(self, account: str, plan: ICloudPlan) -> None:
        account = (account or "").strip()
        if not account:
            raise ValueError("account 不能为空")
        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO account_flags (account, free_plan, plan_name, plan_checked_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account) DO UPDATE SET
                    free_plan=excluded.free_plan, plan_name=excluded.plan_name,
                    plan_checked_at=excluded.plan_checked_at, updated_at=excluded.updated_at
                """,
                (account, int(plan.free_5gb), plan.name, unix_now(), now),
            )
            if plan.free_5gb:
                row = conn.execute(
                    "SELECT selected_accounts_json FROM production_loop_state WHERE id = 1"
                ).fetchone()
                selected = json.loads(row["selected_accounts_json"]) if row else []
                conn.execute(
                    "UPDATE production_loop_state SET selected_accounts_json = ?, updated_at = ? WHERE id = 1",
                    (json.dumps([name for name in selected if name != account], ensure_ascii=False), now),
                )

    def assert_production_ready(self, account: str) -> None:
        self.assert_cookie_ready(account)
        flag = self.get_account_flag(account)
        if flag and flag["free_plan"]:
            raise ICloudFreePlanError(account)

    def get_account_flag(self, account: str) -> dict[str, Any] | None:
        account = (account or "").strip()
        if not account:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM account_flags WHERE account = ?",
                (account,),
            ).fetchone()
        if not row:
            return None
        return {
            "account": row["account"],
            "cookie_invalid": bool(row["cookie_invalid"]),
            "reason": row["reason"] or "",
            "marked_at": int(row["marked_at"] or 0),
            "cleared_at": int(row["cleared_at"] or 0),
            "free_plan": bool(row["free_plan"]),
            "plan_name": row["plan_name"] or "",
            "plan_checked_at": int(row["plan_checked_at"] or 0),
            "updated_at": row["updated_at"] or "",
        }

    def is_cookie_invalid(self, account: str) -> bool:
        flag = self.get_account_flag(account)
        return bool(flag and flag["cookie_invalid"])

    def reconcile_cookie_flag(self, account: Any) -> dict[str, Any] | None:
        """手工更新账户 YAML 后，清除早于文件更新时间的 421 标记。"""
        name = str(getattr(account, "name", "") or "").strip()
        flag = self.get_account_flag(name)
        if not flag or not flag.get("cookie_invalid") or not getattr(account, "ok", False):
            return flag
        source = str(getattr(account, "source", "") or "").strip()
        try:
            modified_at = int(Path(source).stat().st_mtime)
        except (OSError, ValueError):
            return flag
        if modified_at > int(flag.get("marked_at") or 0):
            return self.clear_cookie_invalid(name)
        return flag

    def assert_cookie_ready(self, account: str) -> None:
        from .client import CookieInvalidError

        flag = self.get_account_flag(account)
        if flag and flag["cookie_invalid"]:
            raise CookieInvalidError(
                account,
                reason=flag.get("reason") or "cookie_invalid",
                marked_at=int(flag.get("marked_at") or 0),
            )

    def touch_client(self, client_id: str, user_agent: str = "") -> str:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO client_sync_state (client_id, last_seen_at, user_agent)
                VALUES (?, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at, user_agent=excluded.user_agent
                """,
                ((client_id or "anonymous")[:128], now, (user_agent or "")[:300]),
            )
            conn.commit()
        return now

    def assert_can_create(self, account: str) -> CreateQuota:
        """超限则抛 HMECreateRateLimitError。"""
        q = self.get_create_quota(account)
        if not q.allowed:
            raise HMECreateRateLimitError(
                account=account,
                used=q.used,
                limit=q.limit,
                retry_after_sec=q.retry_after_sec,
                reason="interval" if q.next_produce_at > unix_now() else "hourly",
                next_produce_at=q.next_produce_at,
            )
        return q

    def count_aliases(self, account: str | None = None) -> int:
        with self._connect() as conn:
            if account:
                row = conn.execute(
                    "SELECT COUNT(*) AS total FROM aliases WHERE account = ? OR parent_mail = ?",
                    (account, account),
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) AS total FROM aliases").fetchone()
        return int(row["total"] or 0)

    def get_alias_capacity(
        self,
        account: str,
        limit: int = HME_ACCOUNT_ALIAS_LIMIT,
    ) -> AliasCapacity:
        account = (account or "").strip()
        if not account:
            raise ValueError("account 不能为空")
        since_s = (
            utc_now_dt() - timedelta(seconds=HME_CREATE_WINDOW_SECONDS)
        ).strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            alias_row = conn.execute(
                "SELECT COUNT(*) AS total FROM aliases WHERE account = ? OR parent_mail = ?",
                (account, account),
            ).fetchone()
            pending_row = conn.execute(
                "SELECT COUNT(*) AS total FROM create_claims WHERE account = ? AND claimed_at >= ?",
                (account, since_s),
            ).fetchone()
        alias_count = int(alias_row["total"] or 0)
        pending = int(pending_row["total"] or 0)
        resolved_limit = max(1, int(limit or HME_ACCOUNT_ALIAS_LIMIT))
        return AliasCapacity(
            account=account,
            alias_count=alias_count,
            pending=pending,
            limit=resolved_limit,
            remaining=max(0, resolved_limit - alias_count - pending),
        )

    def assert_alias_capacity(self, account: str) -> AliasCapacity:
        capacity = self.get_alias_capacity(account)
        if not capacity.allowed:
            raise HMEAccountAliasLimitError(
                account=capacity.account,
                alias_count=capacity.alias_count,
                limit=capacity.limit,
                pending=capacity.pending,
            )
        return capacity

    def set_active(self, account: str, anonymous_id: str, active: bool) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE aliases
                SET is_active = ?, updated_at = ?
                WHERE account = ? AND anonymous_id = ?
                """,
                (1 if active else 0, utc_now(), account, anonymous_id),
            )
            conn.commit()
            return cur.rowcount

    def list_aliases(self, account: str | None = None) -> list[AliasRecord]:
        with self._connect() as conn:
            if account:
                rows = conn.execute(
                    "SELECT * FROM aliases WHERE account = ? OR parent_mail = ? ORDER BY id DESC",
                    (account, account),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM aliases ORDER BY id DESC"
                ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get_alias_pool_stats(self) -> dict[str, int]:
        """返回首页号池概览；可用数排除已领取邮箱。"""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(CASE
                        WHEN is_active != 0
                         AND NOT EXISTS (
                             SELECT 1 FROM claim_order_items c
                             WHERE lower(c.hme) = lower(aliases.hme)
                         )
                        THEN 1 ELSE 0 END), 0) AS available,
                    COALESCE(SUM(CASE
                        WHEN substr(upper(trim(COALESCE(label, ''))), 1, 4) = 'CDK_'
                        THEN 1 ELSE 0 END), 0) AS cdk_total,
                    COALESCE(SUM(CASE
                        WHEN is_active != 0
                         AND substr(upper(trim(COALESCE(label, ''))), 1, 4) = 'CDK_'
                         AND NOT EXISTS (
                             SELECT 1 FROM claim_order_items c
                             WHERE lower(c.hme) = lower(aliases.hme)
                         )
                        THEN 1 ELSE 0 END), 0) AS cdk_available,
                    COALESCE((SELECT COUNT(*) FROM claim_order_items), 0) AS claimed
                FROM aliases
                """
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "available": int(row["available"] or 0),
            "cdk_total": int(row["cdk_total"] or 0),
            "cdk_available": int(row["cdk_available"] or 0),
            "claimed": int(row["claimed"] or 0),
        }

    def get_claim_stats(self) -> dict[str, int]:
        """领取页库存：总数 / 可领 / 已领 / 订单数。"""
        pool = self.get_alias_pool_stats()
        with self._connect() as conn:
            orders = int(
                conn.execute("SELECT COUNT(*) AS n FROM claim_orders").fetchone()["n"] or 0
            )
        return {
            "total": int(pool.get("total") or 0),
            "available": int(pool.get("available") or 0),
            "claimed": int(pool.get("claimed") or 0),
            "orders": orders,
        }

    def _new_claim_order_no(self, conn: sqlite3.Connection) -> str:
        for _ in range(12):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            order_no = f"CLM{stamp}{uuid.uuid4().hex[:6].upper()}"
            exists = conn.execute(
                "SELECT 1 FROM claim_orders WHERE order_no = ? LIMIT 1",
                (order_no,),
            ).fetchone()
            if not exists:
                return order_no
        return f"CLM{uuid.uuid4().hex.upper()}"

    def _claim_order_dict(
        self,
        order_row: sqlite3.Row | dict[str, Any],
        item_rows: list[sqlite3.Row] | None = None,
    ) -> dict[str, Any]:
        if isinstance(order_row, sqlite3.Row):
            order = dict(order_row)
        else:
            order = dict(order_row)
        items: list[dict[str, Any]] = []
        for row in item_rows or []:
            item = dict(row)
            items.append(
                {
                    "hme": item.get("hme") or "",
                    "account": item.get("account") or "",
                    "parent_mail": item.get("parent_mail") or "",
                    "label": item.get("label") or "",
                    "cdk": item.get("cdk") or "",
                    "access_token": item.get("access_token") or "",
                }
            )
        return {
            "id": int(order.get("id") or 0),
            "order_no": order.get("order_no") or "",
            "contact_email": order.get("contact_email") or "",
            "note": order.get("note") or "",
            "count": int(order.get("count") or 0),
            "status": order.get("status") or "claimed",
            "deliver_status": order.get("deliver_status") or "pending",
            "deliver_error": order.get("deliver_error") or "",
            "deliver_from": order.get("deliver_from") or "",
            "created_at": order.get("created_at") or "",
            "updated_at": order.get("updated_at") or "",
            "items": items,
            "emails": [it["hme"] for it in items if it.get("hme")],
        }

    def claim_aliases(
        self,
        *,
        count: int,
        contact_email: str,
        note: str = "",
        account: str | None = None,
    ) -> dict[str, Any]:
        """
        原子领取 count 个未占用且仍启用的隐私邮箱。
        领走即占用，不支持自动退回；调用方负责发到常用邮箱。
        """
        want = int(count or 0)
        if want < 1:
            raise ValueError("领取数量至少为 1")
        if want > 500:
            raise ValueError("单次最多领取 500 个")
        contact = (contact_email or "").strip()
        if not contact or "@" not in contact:
            raise ValueError("请填写有效的常用邮箱")
        note_text = (note or "").strip()
        if len(note_text) > 200:
            raise ValueError("备注最多 200 字")
        account_filter = (account or "").strip() or None
        now = utc_now()

        with self._connect() as conn:
            params: list[Any] = []
            sql = """
                SELECT a.id, a.hme, a.account, a.parent_mail, a.label, a.cdk
                FROM aliases a
                WHERE a.is_active != 0
                  AND NOT EXISTS (
                      SELECT 1 FROM claim_order_items c
                      WHERE lower(c.hme) = lower(a.hme)
                  )
            """
            if account_filter:
                sql += " AND (a.account = ? OR a.parent_mail = ?)"
                params.extend([account_filter, account_filter])
            sql += " ORDER BY a.id ASC LIMIT ?"
            params.append(want)
            rows = conn.execute(sql, params).fetchall()
            if len(rows) < want:
                free = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) AS n
                        FROM aliases a
                        WHERE a.is_active != 0
                          AND NOT EXISTS (
                              SELECT 1 FROM claim_order_items c
                              WHERE lower(c.hme) = lower(a.hme)
                          )
                        """
                    ).fetchone()["n"]
                    or 0
                )
                raise ValueError(f"可领邮箱不足：需要 {want} 个，当前仅剩 {free} 个")

            order_no = self._new_claim_order_no(conn)
            cur = conn.execute(
                """
                INSERT INTO claim_orders (
                    order_no, contact_email, note, count, status,
                    deliver_status, deliver_error, deliver_from,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'claimed', 'pending', '', '', ?, ?)
                """,
                (order_no, contact, note_text, want, now, now),
            )
            order_id = int(cur.lastrowid)
            for row in rows:
                token = uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO claim_order_items (
                        order_id, order_no, hme, account, parent_mail,
                        label, cdk, access_token, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order_id,
                        order_no,
                        row["hme"],
                        row["account"] or "",
                        row["parent_mail"] or row["account"] or "",
                        row["label"] or "",
                        row["cdk"] or "",
                        token,
                        now,
                    ),
                )
            order_row = conn.execute(
                "SELECT * FROM claim_orders WHERE id = ?",
                (order_id,),
            ).fetchone()
            item_rows = conn.execute(
                "SELECT * FROM claim_order_items WHERE order_id = ? ORDER BY id ASC",
                (order_id,),
            ).fetchall()
            conn.commit()
        return self._claim_order_dict(order_row, item_rows)

    def update_claim_delivery(
        self,
        order_no: str,
        *,
        deliver_status: str,
        deliver_error: str = "",
        deliver_from: str = "",
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE claim_orders
                SET deliver_status = ?,
                    deliver_error = ?,
                    deliver_from = ?,
                    updated_at = ?
                WHERE order_no = ?
                """,
                (
                    (deliver_status or "").strip() or "pending",
                    (deliver_error or "").strip(),
                    (deliver_from or "").strip(),
                    now,
                    (order_no or "").strip(),
                ),
            )
            if cur.rowcount <= 0:
                return None
            conn.commit()
        return self.get_claim_order(order_no)

    def get_claim_order(self, order_no: str) -> dict[str, Any] | None:
        key = (order_no or "").strip()
        if not key:
            return None
        with self._connect() as conn:
            order_row = conn.execute(
                "SELECT * FROM claim_orders WHERE order_no = ? LIMIT 1",
                (key,),
            ).fetchone()
            if not order_row:
                return None
            item_rows = conn.execute(
                "SELECT * FROM claim_order_items WHERE order_id = ? ORDER BY id ASC",
                (order_row["id"],),
            ).fetchall()
        return self._claim_order_dict(order_row, item_rows)

    def list_claim_orders(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        lim = max(1, min(int(limit or 50), 200))
        off = max(0, int(offset or 0))
        with self._connect() as conn:
            order_rows = conn.execute(
                """
                SELECT * FROM claim_orders
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (lim, off),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for order_row in order_rows:
                item_rows = conn.execute(
                    "SELECT * FROM claim_order_items WHERE order_id = ? ORDER BY id ASC",
                    (order_row["id"],),
                ).fetchall()
                result.append(self._claim_order_dict(order_row, item_rows))
        return result

    def get_claim_item_by_token(self, token: str) -> dict[str, Any] | None:
        key = (token or "").strip()
        if not key:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT i.*, o.contact_email, o.note AS order_note, o.created_at AS order_created_at
                FROM claim_order_items i
                JOIN claim_orders o ON o.id = i.order_id
                WHERE i.access_token = ?
                LIMIT 1
                """,
                (key,),
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        return {
            "hme": data.get("hme") or "",
            "account": data.get("account") or "",
            "parent_mail": data.get("parent_mail") or "",
            "label": data.get("label") or "",
            "cdk": data.get("cdk") or "",
            "access_token": data.get("access_token") or "",
            "order_no": data.get("order_no") or "",
            "contact_email": data.get("contact_email") or "",
            "order_note": data.get("order_note") or "",
            "created_at": data.get("created_at") or "",
            "order_created_at": data.get("order_created_at") or "",
        }

    def get_claim_item_by_email_token(self, email: str, token: str) -> dict[str, Any] | None:
        item = self.get_claim_item_by_token(token)
        if not item:
            return None
        want = (email or "").strip().lower()
        if want and want != str(item.get("hme") or "").strip().lower():
            return None
        return item

    # ---------- CDK 查询 API ----------

    def get_by_cdk(self, cdk: str) -> AliasRecord | None:
        """通过 CDK 定位记录（含母号 + 隐私邮箱）。"""
        key = normalize_cdk(cdk, allow_bare_hex=True)
        if not key:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM aliases WHERE cdk = ? LIMIT 1",
                (key,),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def resolve_cdk(self, cdk: str) -> dict[str, Any] | None:
        """
        CDK -> 键值对业务结果。
        {
          "cdk": "...",
          "hme": "xxx@icloud.com",          # 隐私邮箱
          "parent_mail": "user@icloud.com", # 母号
          "account": "...",
          "anonymous_id": "...",
          "is_active": true,
          "label": "...",
          ...
        }
        """
        rec = self.get_by_cdk(cdk)
        return rec.to_dict() if rec else None

    def get_by_hme(self, hme: str) -> list[AliasRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM aliases WHERE hme = ? ORDER BY id DESC",
                (hme.strip(),),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def list_by_parent(self, parent_mail: str) -> list[AliasRecord]:
        """按母号列出其下所有隐私邮箱。"""
        p = parent_mail.strip()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM aliases
                WHERE parent_mail = ? OR account = ?
                ORDER BY id DESC
                """,
                (p, p),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def cdk_map(self, parent_mail: str | None = None) -> dict[str, dict[str, str]]:
        """
        导出 CDK 映射表：
          { "CDK_xxx": {"hme": "...", "parent_mail": "..."}, ... }
        """
        rows = self.list_by_parent(parent_mail) if parent_mail else self.list_aliases()
        out: dict[str, dict[str, str]] = {}
        for r in rows:
            if not r.cdk:
                continue
            out[r.cdk] = {
                "hme": r.hme,
                "parent_mail": r.parent_mail or r.account,
                "anonymous_id": r.anonymous_id,
                "is_active": "1" if r.is_active else "0",
            }
        return out

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> AliasRecord:
        keys = set(row.keys())
        cdk = row["cdk"] if "cdk" in keys else ""
        parent = row["parent_mail"] if "parent_mail" in keys else ""
        return AliasRecord(
            id=row["id"],
            cdk=cdk or extract_cdk_from_label(row["label"]) or "",
            parent_mail=parent or row["account"] or "",
            account=row["account"],
            hme=row["hme"],
            label=row["label"],
            anonymous_id=row["anonymous_id"],
            is_active=row["is_active"],
            create_timestamp=row["create_timestamp"],
            note=row["note"],
            source=row["source"],
            raw_json=row["raw_json"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ---------- 邮件 ----------

    def upsert_mail(
        self,
        *,
        account: str,
        uid: str,
        mailbox: str = "INBOX",
        parent_mail: str = "",
        message_id: str = "",
        alias_hme: str = "",
        from_name: str = "",
        from_addr: str = "",
        sender_addr: str = "",
        return_path: str = "",
        received_spf: str = "",
        envelope_from: str = "",
        is_relayed: bool = False,
        relay_label: str = "",
        to_addr: str = "",
        delivered_to: str = "",
        subject: str = "",
        mail_type: str = "other",
        code: str = "",
        summary: str = "",
        date_header: str = "",
        date_utc: str = "",
        internaldate: str = "",
        size: int | None = None,
        flags: list[str] | None = None,
        is_seen: bool = False,
        attachments: list[dict[str, Any]] | None = None,
        body_text_len: int = 0,
        body_html_len: int = 0,
        content_type: str = "",
    ) -> tuple[int, bool]:
        """
        写入/更新一封邮件（只存元数据与分类结果）。
        返回 (mail_id, is_new)。唯一键为 (account, mailbox, uid)。
        """
        now = utc_now()
        atts = attachments or []
        flags_json = json.dumps(flags or [], ensure_ascii=False)
        atts_json = json.dumps(atts, ensure_ascii=False, default=str)

        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM mails WHERE account = ? AND mailbox = ? AND uid = ?",
                (account, mailbox, str(uid)),
            ).fetchone()
            is_new = row is None
            conn.execute(
                """
                INSERT INTO mails (
                    account, parent_mail, mailbox, uid, message_id, alias_hme,
                    from_name, from_addr, sender_addr, return_path, received_spf, envelope_from,
                    is_relayed, relay_label,
                    to_addr, delivered_to, subject, mail_type, code, summary,
                    date_header, date_utc, internaldate, size, flags_json, is_seen,
                    has_attachment, attachments_json, body_text_len, body_html_len,
                    content_type, fetched_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account, mailbox, uid) DO UPDATE SET
                    parent_mail=CASE WHEN excluded.parent_mail != '' THEN excluded.parent_mail ELSE mails.parent_mail END,
                    message_id=CASE WHEN excluded.message_id != '' THEN excluded.message_id ELSE mails.message_id END,
                    alias_hme=CASE WHEN excluded.alias_hme != '' THEN excluded.alias_hme ELSE mails.alias_hme END,
                    from_name=excluded.from_name,
                    from_addr=excluded.from_addr,
                    sender_addr=excluded.sender_addr,
                    return_path=excluded.return_path,
                    received_spf=CASE WHEN excluded.received_spf != '' THEN excluded.received_spf ELSE mails.received_spf END,
                    envelope_from=CASE WHEN excluded.envelope_from != '' THEN excluded.envelope_from ELSE mails.envelope_from END,
                    is_relayed=excluded.is_relayed,
                    relay_label=excluded.relay_label,
                    to_addr=excluded.to_addr,
                    delivered_to=excluded.delivered_to,
                    subject=excluded.subject,
                    mail_type=excluded.mail_type,
                    code=CASE WHEN excluded.code != '' THEN excluded.code ELSE mails.code END,
                    summary=excluded.summary,
                    date_header=excluded.date_header,
                    date_utc=CASE WHEN excluded.date_utc != '' THEN excluded.date_utc ELSE mails.date_utc END,
                    internaldate=CASE WHEN excluded.internaldate != '' THEN excluded.internaldate ELSE mails.internaldate END,
                    size=COALESCE(excluded.size, mails.size),
                    flags_json=excluded.flags_json,
                    is_seen=excluded.is_seen,
                    has_attachment=excluded.has_attachment,
                    attachments_json=excluded.attachments_json,
                    body_text_len=excluded.body_text_len,
                    body_html_len=excluded.body_html_len,
                    content_type=excluded.content_type,
                    fetched_at=excluded.fetched_at,
                    updated_at=excluded.updated_at
                """,
                (
                    account,
                    (parent_mail or account or "").strip(),
                    mailbox,
                    str(uid),
                    message_id,
                    alias_hme,
                    from_name,
                    from_addr,
                    sender_addr,
                    return_path,
                    received_spf,
                    envelope_from,
                    1 if is_relayed else 0,
                    relay_label,
                    to_addr,
                    delivered_to,
                    subject,
                    mail_type or "other",
                    code,
                    summary,
                    date_header,
                    date_utc,
                    internaldate,
                    size,
                    flags_json,
                    1 if is_seen else 0,
                    1 if atts else 0,
                    atts_json,
                    body_text_len,
                    body_html_len,
                    content_type,
                    now,
                    now,
                    now,
                ),
            )
            mail_row = conn.execute(
                "SELECT id FROM mails WHERE account = ? AND mailbox = ? AND uid = ?",
                (account, mailbox, str(uid)),
            ).fetchone()
            conn.commit()
        return (int(mail_row["id"]) if mail_row else 0), is_new

    @staticmethod
    def _mail_filters(
        account: str | None,
        alias_hme: str | None,
        mail_type: str | None,
        mailbox: str | None,
        q: str | None,
    ) -> tuple[str, list[Any]]:
        where: list[str] = []
        params: list[Any] = []
        if account:
            where.append("(account = ? OR parent_mail = ?)")
            params.extend([account, account])
        if alias_hme:
            where.append("alias_hme = ?")
            params.append(alias_hme.strip().lower())
        if mail_type:
            where.append("mail_type = ?")
            params.append(mail_type)
        if mailbox:
            where.append("mailbox = ?")
            params.append(mailbox)
        if q:
            like = f"%{q.strip()}%"
            where.append(
                "(subject LIKE ? OR from_addr LIKE ? OR from_name LIKE ? "
                "OR alias_hme LIKE ? OR summary LIKE ? OR code LIKE ?)"
            )
            params.extend([like] * 6)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        return clause, params

    def list_mails(
        self,
        *,
        account: str | None = None,
        alias_hme: str | None = None,
        mail_type: str | None = None,
        mailbox: str | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MailRecord]:
        clause, params = self._mail_filters(account, alias_hme, mail_type, mailbox, q)
        lim = max(1, min(int(limit or 50), 500))
        off = max(0, int(offset or 0))
        # date_utc 可能为空（Date 头缺失/不可解析），回落到 fetched_at 保证排序稳定
        # 列表不需要纯文本缓存，避免把大字段拖进邮箱池。
        select_sql = (
            "SELECT id, account, parent_mail, mailbox, uid, message_id, alias_hme, "
            "from_name, from_addr, sender_addr, return_path, received_spf, envelope_from, "
            "is_relayed, relay_label, to_addr, delivered_to, subject, mail_type, code, summary, "
            "date_header, date_utc, internaldate, size, flags_json, is_seen, has_attachment, "
            "attachments_json, body_text_len, body_html_len, content_type, fetched_at, created_at, updated_at "
            "FROM mails"
        )
        sql = (
            select_sql + clause
            + " ORDER BY CASE WHEN date_utc != '' THEN date_utc ELSE fetched_at END DESC,"
              " id DESC LIMIT ? OFFSET ?"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, (*params, lim, off)).fetchall()
        return [self._row_to_mail(r) for r in rows]

    def count_mails(
        self,
        *,
        account: str | None = None,
        alias_hme: str | None = None,
        mail_type: str | None = None,
        mailbox: str | None = None,
        q: str | None = None,
    ) -> int:
        clause, params = self._mail_filters(account, alias_hme, mail_type, mailbox, q)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM mails" + clause, params
            ).fetchone()
        return int(row["n"]) if row else 0

    def count_mails_by_type(
        self,
        account: str | None = None,
        alias_hme: str | None = None,
        *,
        mailbox: str | None = None,
        q: str | None = None,
    ) -> dict[str, int]:
        clause, params = self._mail_filters(account, alias_hme, None, mailbox, q)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT mail_type, COUNT(*) AS n FROM mails" + clause
                + " GROUP BY mail_type",
                params,
            ).fetchall()
        return {str(r["mail_type"] or "other"): int(r["n"]) for r in rows}

    def count_mails_by_mailbox(
        self,
        account: str | None = None,
        alias_hme: str | None = None,
        *,
        q: str | None = None,
    ) -> dict[str, int]:
        clause, params = self._mail_filters(account, alias_hme, None, None, q)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT mailbox, COUNT(*) AS n FROM mails" + clause
                + " GROUP BY mailbox",
                params,
            ).fetchall()
        return {str(r["mailbox"] or "INBOX"): int(r["n"]) for r in rows}

    def count_mails_by_alias(self, account: str | None = None) -> dict[str, dict[str, Any]]:
        """每个收件别名的邮件数与最近来信时间（池子卡片徽章用）。"""
        clause, params = self._mail_filters(account, None, None, None, None)
        extra = "alias_hme != ''"
        clause = (clause + " AND " + extra) if clause else (" WHERE " + extra)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT alias_hme, COUNT(*) AS n, "
                "MAX(CASE WHEN date_utc != '' THEN date_utc ELSE fetched_at END) AS last_at "
                "FROM mails" + clause + " GROUP BY alias_hme",
                params,
            ).fetchall()
        return {
            str(r["alias_hme"]): {"count": int(r["n"]), "last_mail_at": r["last_at"] or ""}
            for r in rows
        }

    def get_mail(self, account: str, mailbox: str, uid: str) -> MailRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM mails WHERE account = ? AND mailbox = ? AND uid = ? LIMIT 1",
                (account, mailbox, str(uid)),
            ).fetchone()
        return self._row_to_mail(row) if row else None

    def get_mail_by_id(self, mail_id: int) -> MailRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM mails WHERE id = ? LIMIT 1", (int(mail_id),)
            ).fetchone()
        return self._row_to_mail(row) if row else None

    def save_mail_body_text(self, account: str, mailbox: str, uid: str, body_text: str) -> None:
        """只更新纯文本缓存，不碰 flags / 附件。"""
        text = body_text or ""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE mails
                SET body_text = ?, body_text_len = ?, updated_at = ?
                WHERE account = ? AND mailbox = ? AND uid = ?
                """,
                (text, len(text), utc_now(), account, mailbox, str(uid)),
            )
            conn.commit()

    # ---------- 增量收取水位 ----------

    def get_sync_state(self, account: str, mailbox: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM mail_sync_state WHERE account = ? AND mailbox = ?",
                (account, mailbox),
            ).fetchone()
        if not row:
            return {
                "account": account,
                "mailbox": mailbox,
                "last_uid": 0,
                "sync_cursor": "",
                "last_sync_at": "",
                "last_status": "",
                "last_error": "",
                "total_saved": 0,
            }
        return dict(row)

    def set_sync_state(
        self,
        account: str,
        mailbox: str,
        *,
        last_uid: int,
        sync_cursor: str | None = None,
        status: str = "ok",
        error: str = "",
        saved_delta: int = 0,
    ) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO mail_sync_state (
                    account, mailbox, last_uid, sync_cursor, last_sync_at,
                    last_status, last_error, total_saved
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account, mailbox) DO UPDATE SET
                    last_uid=MAX(excluded.last_uid, mail_sync_state.last_uid),
                    sync_cursor=CASE
                        WHEN excluded.sync_cursor != '' THEN excluded.sync_cursor
                        ELSE mail_sync_state.sync_cursor
                    END,
                    last_sync_at=excluded.last_sync_at,
                    last_status=excluded.last_status,
                    last_error=excluded.last_error,
                    total_saved=mail_sync_state.total_saved + excluded.total_saved
                """,
                (
                    account,
                    mailbox,
                    max(0, int(last_uid)),
                    str(sync_cursor or ""),
                    now,
                    status,
                    error,
                    max(0, int(saved_delta)),
                ),
            )
            conn.commit()

    def list_sync_states(self, account: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if account:
                rows = conn.execute(
                    "SELECT account, mailbox, last_uid, last_sync_at, last_status, "
                    "last_error, total_saved FROM mail_sync_state "
                    "WHERE account = ? ORDER BY mailbox",
                    (account,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT account, mailbox, last_uid, last_sync_at, last_status, "
                    "last_error, total_saved FROM mail_sync_state ORDER BY account, mailbox"
                ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 分组（预留，UI 后续再做）----------

    def create_group(
        self, name: str, *, account: str = "", color: str = "", note: str = ""
    ) -> dict[str, Any]:
        label = (name or "").strip()
        if not label:
            raise ValueError("分组名称不能为空")
        now = utc_now()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO alias_groups (name, account, color, note, sort_order, created_at, updated_at)
                VALUES (?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(account, name) DO UPDATE SET
                    color=CASE WHEN excluded.color != '' THEN excluded.color ELSE alias_groups.color END,
                    note=CASE WHEN excluded.note != '' THEN excluded.note ELSE alias_groups.note END,
                    updated_at=excluded.updated_at
                """,
                (label, (account or "").strip(), color, note, now, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM alias_groups WHERE account = ? AND name = ?",
                ((account or "").strip(), label),
            ).fetchone()
            _ = cur
        return dict(row) if row else {}

    def list_groups(self, account: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if account:
                rows = conn.execute(
                    "SELECT g.*, "
                    "(SELECT COUNT(*) FROM alias_group_members m WHERE m.group_id = g.id) AS member_count "
                    "FROM alias_groups g WHERE g.account = ? OR g.account = '' "
                    "ORDER BY g.sort_order, g.id",
                    (account,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT g.*, "
                    "(SELECT COUNT(*) FROM alias_group_members m WHERE m.group_id = g.id) AS member_count "
                    "FROM alias_groups g ORDER BY g.sort_order, g.id"
                ).fetchall()
        return [dict(r) for r in rows]

    def delete_group(self, group_id: int) -> bool:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM alias_group_members WHERE group_id = ?", (int(group_id),)
            )
            cur = conn.execute(
                "DELETE FROM alias_groups WHERE id = ?", (int(group_id),)
            )
            conn.commit()
            return cur.rowcount > 0

    def add_group_member(self, group_id: int, hme: str) -> bool:
        key = (hme or "").strip().lower()
        if not key:
            raise ValueError("hme 不能为空")
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM alias_groups WHERE id = ?", (int(group_id),)
            ).fetchone()
            if not exists:
                raise LookupError(f"分组不存在: {group_id}")
            cur = conn.execute(
                "INSERT OR IGNORE INTO alias_group_members (group_id, hme, created_at) "
                "VALUES (?, ?, ?)",
                (int(group_id), key, utc_now()),
            )
            conn.commit()
            return cur.rowcount > 0

    def remove_group_member(self, group_id: int, hme: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM alias_group_members WHERE group_id = ? AND hme = ?",
                (int(group_id), (hme or "").strip().lower()),
            )
            conn.commit()
            return cur.rowcount > 0

    def groups_for_hme(self, hme: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT g.* FROM alias_groups g "
                "JOIN alias_group_members m ON m.group_id = g.id "
                "WHERE m.hme = ? ORDER BY g.sort_order, g.id",
                ((hme or "").strip().lower(),),
            ).fetchall()
        return [dict(r) for r in rows]

    def group_members_map(self) -> dict[str, list[dict[str, Any]]]:
        """hme -> 所属分组列表，供别名列表一次性附加分组信息。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT m.hme, g.id, g.name, g.color FROM alias_group_members m "
                "JOIN alias_groups g ON g.id = m.group_id "
                "ORDER BY g.sort_order, g.id"
            ).fetchall()
        out: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            out.setdefault(str(r["hme"]), []).append(
                {"id": r["id"], "name": r["name"], "color": r["color"]}
            )
        return out

    @staticmethod
    def _row_to_mail(row: sqlite3.Row) -> MailRecord:
        return MailRecord(
            id=row["id"],
            account=row["account"],
            parent_mail=row["parent_mail"],
            mailbox=row["mailbox"],
            uid=row["uid"],
            message_id=row["message_id"],
            alias_hme=row["alias_hme"],
            from_name=row["from_name"],
            from_addr=row["from_addr"],
            sender_addr=row["sender_addr"],
            return_path=row["return_path"],
            received_spf=row["received_spf"] if "received_spf" in row.keys() else "",
            envelope_from=row["envelope_from"] if "envelope_from" in row.keys() else "",
            is_relayed=row["is_relayed"],
            relay_label=row["relay_label"],
            to_addr=row["to_addr"],
            delivered_to=row["delivered_to"],
            subject=row["subject"],
            mail_type=row["mail_type"],
            code=row["code"],
            summary=row["summary"],
            date_header=row["date_header"],
            date_utc=row["date_utc"],
            internaldate=row["internaldate"],
            size=row["size"],
            flags_json=row["flags_json"],
            is_seen=row["is_seen"],
            has_attachment=row["has_attachment"],
            attachments_json=row["attachments_json"],
            body_text_len=row["body_text_len"],
            body_html_len=row["body_html_len"],
            content_type=row["content_type"],
            body_text=row["body_text"] if "body_text" in row.keys() else "",
            fetched_at=row["fetched_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
