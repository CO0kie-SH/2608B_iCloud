from __future__ import annotations

"""领取邮箱取码：按 access_token 查本地验证码，可选触发母号增量收信。"""

import re
import time
from datetime import datetime, timezone
from typing import Any


_CODE_RE = re.compile(r"\b(\d{6})\b")


def _parse_mail_ts(value: str | int | float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        return number if number > 0 else None
    text = str(value or "").strip()
    if not text:
        return None
    # unix 数字字符串
    if re.fullmatch(r"\d{10,13}", text):
        number = float(text)
        if number > 10_000_000_000:
            number /= 1000.0
        return number
    # 常见 UTC 文本
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            dt = datetime.strptime(text.replace("+00:00", "+0000"), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def extract_six_digit_code(*parts: str) -> str:
    blob = " ".join(str(p or "") for p in parts)
    # 优先带验证码语义的上下文
    patterns = [
        r"(?:OpenAI|ChatGPT|verification|verify|code|验证码|登录码)[^\d]{0,80}(\d{6})",
        r"\b(\d{6})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, blob, flags=re.I)
        if match:
            return match.group(1)
    return ""


def _mail_timestamp(rec: Any) -> float | None:
    for attr in ("date_utc", "internaldate", "fetched_at", "created_at"):
        ts = _parse_mail_ts(getattr(rec, attr, None) if not isinstance(rec, dict) else rec.get(attr))
        if ts is not None:
            return ts
    return None


def find_latest_code(
    db: Any,
    *,
    email: str,
    after: float = 0.0,
    max_age_seconds: int = 600,
    allow_stale: bool = False,
) -> dict[str, Any]:
    """从本地 mails 表找该隐私邮箱最近验证码。"""
    hme = (email or "").strip()
    now = time.time()
    after_ts = max(0.0, float(after or 0.0))
    max_age = max(0, int(max_age_seconds or 0))

    records = db.list_mails(alias_hme=hme, mail_type="code", limit=30, offset=0)
    # 再兜底扫最近邮件，防止 type 还没标成 code
    if not records:
        records = db.list_mails(alias_hme=hme, limit=30, offset=0)

    best: tuple[float, Any, str] | None = None
    for rec in records:
        code = str(getattr(rec, "code", "") or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            code = extract_six_digit_code(
                getattr(rec, "subject", ""),
                getattr(rec, "summary", ""),
                getattr(rec, "code", ""),
            )
        if not code:
            continue
        ts = _mail_timestamp(rec) or 0.0
        if after_ts and ts and ts < after_ts:
            continue
        if after_ts and not ts:
            # 强制 after 时，没有时间戳的不算
            continue
        if best is None or ts >= best[0]:
            best = (ts, rec, code)

    if best is None:
        return {
            "ok": True,
            "code": "",
            "email": hme,
            "lookup_status": "no_recent_code",
            "stale": False,
            "mail": None,
        }

    ts, rec, code = best
    stale = False
    if max_age > 0 and ts > 0 and (now - ts) > max_age:
        stale = True
    if stale and not allow_stale:
        return {
            "ok": True,
            "code": "",
            "email": hme,
            "lookup_status": "no_recent_code",
            "stale": True,
            "mail": {
                "subject": getattr(rec, "subject", "") or "",
                "from_addr": getattr(rec, "from_addr", "") or "",
                "date_utc": getattr(rec, "date_utc", "") or "",
                "timestamp": ts,
            },
        }

    return {
        "ok": True,
        "code": code,
        "email": hme,
        "lookup_status": "ok",
        "stale": stale,
        "mail": {
            "subject": getattr(rec, "subject", "") or "",
            "from_addr": getattr(rec, "from_addr", "") or "",
            "from_name": getattr(rec, "from_name", "") or "",
            "summary": getattr(rec, "summary", "") or "",
            "date_utc": getattr(rec, "date_utc", "") or "",
            "internaldate": getattr(rec, "internaldate", "") or "",
            "mailbox": getattr(rec, "mailbox", "") or "",
            "uid": getattr(rec, "uid", "") or "",
            "account": getattr(rec, "account", "") or "",
            "timestamp": ts,
        },
    }


def maybe_sync_account(db: Any, account_name: str) -> dict[str, Any]:
    """尽力增量收信；失败不抛给注册机，只记状态。"""
    name = (account_name or "").strip()
    if not name:
        return {"synced": False, "error": "missing account"}
    try:
        from web.deps import get_accounts, get_sync_service, resolve_account

        # resolve_account 依赖全局账户；这里尽量找同名
        try:
            acc = resolve_account(name)
        except Exception:
            acc = None
            for item in get_accounts():
                if str(getattr(item, "name", "") or "").lower() == name.lower():
                    acc = item
                    break
        if acc is None:
            return {"synced": False, "error": f"account not found: {name}"}
        if not getattr(acc, "mail_ready", False):
            return {"synced": False, "error": "mail not ready"}
        service = get_sync_service()
        # MailSyncService 常见入口：sync_account
        if hasattr(service, "sync_account"):
            result = service.sync_account(acc)
            return {"synced": True, "result": str(result)[:300]}
        if hasattr(service, "sync"):
            result = service.sync(acc)
            return {"synced": True, "result": str(result)[:300]}
        return {"synced": False, "error": "sync method missing"}
    except Exception as exc:
        return {"synced": False, "error": f"{type(exc).__name__}: {exc}"}
