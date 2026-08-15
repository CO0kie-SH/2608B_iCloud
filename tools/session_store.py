from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings


def _safe_name(value: str, max_len: int = 80) -> str:
    text = re.sub(r"[^\w.@+-]+", "_", (value or "").strip())
    return (text[:max_len] if text else "account")


def cookie_store_dir(settings: Settings) -> Path:
    path = Path(settings.base_dir) / "db" / "cookie"
    path.mkdir(parents=True, exist_ok=True)
    return path


def session_file(settings: Settings, account: str) -> Path:
    return cookie_store_dir(settings) / f"{_safe_name(account)}.json"


def snapshot_dir(settings: Settings, account: str) -> Path:
    path = cookie_store_dir(settings) / _safe_name(account)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def playwright_cookies_to_records(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in raw or []:
        name = str(item.get("name") or "").strip()
        if not name or item.get("value") is None:
            continue
        records.append(
            {
                "name": name,
                "value": str(item.get("value")),
                "domain": str(item.get("domain") or ""),
                "path": str(item.get("path") or "/"),
                "expires": item.get("expires", -1),
                "httpOnly": bool(item.get("httpOnly")),
                "secure": bool(item.get("secure")),
                "sameSite": item.get("sameSite") or "Lax",
            }
        )
    return records


def save_session(
    settings: Settings,
    account: str,
    cookies: list[dict[str, Any]],
    *,
    source_url: str = "",
) -> Path:
    path = session_file(settings, account)
    payload = {
        "account": account,
        "updated_at": _now_iso(),
        "source_url": source_url,
        "count": len(cookies),
        "cookies": cookies,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def save_page_snapshot(
    settings: Settings,
    account: str,
    cookies: list[dict[str, Any]],
    *,
    url: str = "",
    title: str = "",
    stage: str = "",
) -> Path:
    folder = snapshot_dir(settings, account)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    host = re.sub(r"[^\w.-]+", "_", (url.split("/")[2] if "://" in url else url) or "page")
    path = folder / f"{stamp}-{host[:40]}.json"
    payload = {
        "account": account,
        "utc": _now_iso(),
        "stage": stage,
        "url": url,
        "title": title,
        "count": len(cookies),
        "names": [c.get("name") for c in cookies],
        "cookies": cookies,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_session(settings: Settings, account: str) -> list[dict[str, Any]]:
    path = session_file(settings, account)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    cookies = data.get("cookies") if isinstance(data, dict) else None
    return cookies if isinstance(cookies, list) else []


def inject_session(context: Any, cookies: list[dict[str, Any]]) -> int:
    cleaned: list[dict[str, Any]] = []
    for item in cookies:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        rec = {
            "name": item["name"],
            "value": str(item.get("value") or ""),
            "domain": item.get("domain") or "",
            "path": item.get("path") or "/",
            "httpOnly": bool(item.get("httpOnly")),
            "secure": bool(item.get("secure")),
        }
        expires = item.get("expires")
        if isinstance(expires, (int, float)) and expires > 0:
            rec["expires"] = float(expires)
        same = item.get("sameSite")
        if same in {"Strict", "Lax", "None"}:
            rec["sameSite"] = same
        if not rec["domain"]:
            continue
        cleaned.append(rec)
    if not cleaned:
        return 0
    context.add_cookies(cleaned)
    return len(cleaned)
