from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path


_LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _safe_token(value: str, max_len: int = 40) -> str:
    s = re.sub(r"[^\w.@+-]+", "_", (value or "").strip())
    return (s[:max_len] if s else "unknown")


def setup_logger(
    name: str,
    log_dir: Path,
    *,
    file_prefix: str,
    account: str | None = None,
    level: int = logging.DEBUG,
    console_level: int = logging.INFO,
) -> tuple[logging.Logger, Path]:
    """
    控制台 INFO + 文件 DEBUG。返回 (logger, log_file_path)。
    不在日志里写 cookie 值 / 密码 / 验证码。
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    parts = [file_prefix]
    if account:
        parts.append(_safe_token(account))
    parts.append(ts)
    log_path = log_dir / ("-".join(parts) + ".log")

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.handlers.clear()
    logger.propagate = False

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(console_level)
    ch.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
    logger.addHandler(ch)

    logger.debug("log_file=%s", log_path)
    return logger, log_path


def cookie_keys_summary(keys: list[str] | set[str], *, limit: int = 24) -> str:
    ordered = sorted(keys)
    if len(ordered) <= limit:
        return ",".join(ordered)
    head = ",".join(ordered[:limit])
    return f"{head},...(+{len(ordered) - limit})"
