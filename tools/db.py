from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


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


class AliasDB:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
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
    ) -> None:
        now = utc_now()
        raw_json = json.dumps(raw, ensure_ascii=False, default=str) if raw is not None else ""
        with self._connect() as conn:
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
            conn.commit()

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
