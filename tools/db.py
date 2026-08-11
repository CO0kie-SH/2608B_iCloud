from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .rate_limit import (
    HME_CREATE_LIMIT_PER_HOUR,
    HME_CREATE_WINDOW_SECONDS,
    HMECreateRateLimitError,
)

# CDK_ + 8~64 hex
CDK_RE = re.compile(r"^CDK_[0-9a-fA-F]{8,64}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


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
class CreateQuota:
    account: str
    used: int
    limit: int
    window_seconds: int
    remaining: int
    retry_after_sec: int
    recent: list[dict[str, str]]

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

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

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
                    created_at TEXT NOT NULL
                )
                """
            )
            ev_cols = self._table_columns(conn, "create_events")
            if "cdk" not in ev_cols:
                conn.execute(
                    "ALTER TABLE create_events ADD COLUMN cdk TEXT NOT NULL DEFAULT ''"
                )
            if "parent_mail" not in ev_cols:
                conn.execute(
                    "ALTER TABLE create_events ADD COLUMN parent_mail TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_create_events_account_time "
                "ON create_events(account, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_create_events_cdk "
                "ON create_events(cdk)"
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
        cdk_val = normalize_cdk(cdk) or extract_cdk_from_label(label)
        parent = (parent_mail or account or "").strip()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO create_events (account, parent_mail, hme, cdk, label, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (account, parent, hme, cdk_val, label, now),
            )
            conn.commit()
        return now

    def get_create_quota(
        self,
        account: str,
        limit: int = HME_CREATE_LIMIT_PER_HOUR,
        window_seconds: int = HME_CREATE_WINDOW_SECONDS,
    ) -> CreateQuota:
        now = utc_now_dt()
        since = now - timedelta(seconds=window_seconds)
        since_s = since.strftime("%Y-%m-%d %H:%M:%S")

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT hme, label, cdk, created_at FROM create_events
                WHERE account = ? AND created_at >= ?
                ORDER BY created_at ASC
                """,
                (account, since_s),
            ).fetchall()

        recent = [
            {
                "hme": r["hme"],
                "label": r["label"],
                "cdk": r["cdk"] if "cdk" in r.keys() else "",
                "created_at": r["created_at"],
            }
            for r in rows
        ]
        used = len(recent)
        remaining = max(0, limit - used)
        retry_after = 0
        if used >= limit and recent:
            oldest = parse_utc(recent[0]["created_at"])
            if oldest:
                unlock_at = oldest + timedelta(seconds=window_seconds)
                retry_after = max(0, int((unlock_at - now).total_seconds()))

        return CreateQuota(
            account=account,
            used=used,
            limit=limit,
            window_seconds=window_seconds,
            remaining=remaining,
            retry_after_sec=retry_after,
            recent=recent,
        )

    def assert_can_create(self, account: str) -> CreateQuota:
        """超限则抛 HMECreateRateLimitError。"""
        q = self.get_create_quota(account)
        if not q.allowed:
            raise HMECreateRateLimitError(
                account=account,
                used=q.used,
                limit=q.limit,
                retry_after_sec=q.retry_after_sec,
            )
        return q

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
