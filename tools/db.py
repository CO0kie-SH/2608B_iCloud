from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .rate_limit import (
    HME_CREATE_LIMIT_PER_HOUR,
    HME_CREATE_WINDOW_SECONDS,
    HMECreateRateLimitError,
)


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


@dataclass
class AliasRecord:
    id: int
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
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_aliases_account ON aliases(account)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_aliases_anonymous_id ON aliases(anonymous_id)"
            )
            # 创建事件表：专门用于限流（记录每次成功创建的本地时间）
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS create_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account TEXT NOT NULL,
                    hme TEXT NOT NULL DEFAULT '',
                    label TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_create_events_account_time "
                "ON create_events(account, created_at)"
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
        preserve_created_at: bool = True,
    ) -> None:
        now = utc_now()
        raw_json = json.dumps(raw, ensure_ascii=False, default=str) if raw is not None else ""
        with self._connect() as conn:
            if preserve_created_at:
                conn.execute(
                    """
                    INSERT INTO aliases (
                        account, hme, label, anonymous_id, is_active, create_timestamp,
                        note, source, raw_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account, hme) DO UPDATE SET
                        label=excluded.label,
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
            else:
                conn.execute(
                    """
                    INSERT INTO aliases (
                        account, hme, label, anonymous_id, is_active, create_timestamp,
                        note, source, raw_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account, hme) DO UPDATE SET
                        label=excluded.label,
                        anonymous_id=CASE
                            WHEN excluded.anonymous_id != '' THEN excluded.anonymous_id
                            ELSE aliases.anonymous_id
                        END,
                        is_active=excluded.is_active,
                        create_timestamp=COALESCE(excluded.create_timestamp, aliases.create_timestamp),
                        note=CASE WHEN excluded.note != '' THEN excluded.note ELSE aliases.note END,
                        source=CASE WHEN excluded.source != '' THEN excluded.source ELSE aliases.source END,
                        raw_json=CASE WHEN excluded.raw_json != '' THEN excluded.raw_json ELSE aliases.raw_json END,
                        created_at=excluded.created_at,
                        updated_at=excluded.updated_at
                    """,
                    (
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

    def record_create_event(self, account: str, hme: str = "", label: str = "") -> str:
        """记录一次成功创建（用于 1 小时限流）。返回 created_at。"""
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO create_events (account, hme, label, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (account, hme, label, now),
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
                SELECT hme, label, created_at FROM create_events
                WHERE account = ? AND created_at >= ?
                ORDER BY created_at ASC
                """,
                (account, since_s),
            ).fetchall()

        recent = [
            {"hme": r["hme"], "label": r["label"], "created_at": r["created_at"]}
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
                    "SELECT * FROM aliases WHERE account = ? ORDER BY id DESC",
                    (account,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM aliases ORDER BY id DESC"
                ).fetchall()
        return [self._row_to_record(r) for r in rows]

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> AliasRecord:
        return AliasRecord(
            id=row["id"],
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
