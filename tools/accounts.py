from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .cookies import OPTIONAL_COOKIE_KEYS, REQUIRED_COOKIE_KEYS, missing_required_keys, parse_cookie_keys


@dataclass
class Account:
    name: str
    mail: str
    app_password: str
    cookies: str
    source: str
    keys: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    format_errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.format_errors and bool(self.cookies)

    @property
    def mail_ready(self) -> bool:
        return bool(self.mail and self.app_password)

    def summary(self) -> str:
        parts: list[str] = []
        if self.format_errors:
            parts.append(f"格式: {', '.join(self.format_errors)}")
        if self.missing:
            parts.append(f"cookie缺: {', '.join(self.missing)}")
        if not self.cookies:
            parts.append("无cookie")
        status = "OK" if self.ok else "; ".join(parts) if parts else "ERR"
        pwd = "pwd=Y" if self.app_password else "pwd=N"
        return (
            f"{self.name} <{self.mail or '-'}> [{status}] {pwd} "
            f"keys={len(self.keys)} <- {Path(self.source).name}"
        )


def resolve_account_files(raw: str, base_dir: Path) -> list[Path]:
    """解析 ACCOUNTS_FILES：单文件 / 多文件 / 目录。"""
    paths: list[Path] = []
    if not raw or not raw.strip():
        return paths

    for part in re.split(r"[,;]+", raw):
        part = part.strip().strip('"').strip("'")
        if not part:
            continue
        p = Path(part)
        if not p.is_absolute():
            p = base_dir / p

        if p.is_dir():
            for f in sorted(p.glob("*.txt")):
                if f.name.endswith(".example") or f.name.startswith("."):
                    continue
                paths.append(f)
        else:
            paths.append(p)
    return paths


def _first_data_line(path: Path) -> str | None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        return line
    return None


def parse_accounts_file(path: Path) -> list[Account]:
    """
    三段式账户文件：
      文件名 = 主邮箱（可带 .txt）
      内容   = MAIL|APPPWD|COOKIE
    """
    if not path.exists():
        return []

    stem = path.stem.strip()
    line = _first_data_line(path)
    format_errors: list[str] = []

    if not line:
        return [
            Account(
                name=stem,
                mail=stem if "@" in stem else "",
                app_password="",
                cookies="",
                source=str(path),
                format_errors=["文件为空"],
            )
        ]

    parts = line.split("|", 2)
    if len(parts) == 3:
        mail, app_password, cookies = (p.strip() for p in parts)
    elif len(parts) == 2:
        # 兼容旧：name|cookie 或 mail|cookie
        left, right = parts[0].strip(), parts[1].strip()
        if "X-APPLE" in right or "=" in right:
            mail, app_password, cookies = left, "", right
            format_errors.append("缺APPPWD段，请改为 MAIL|APPPWD|COOKIE")
        else:
            mail, app_password, cookies = left, right, ""
            format_errors.append("缺COOKIE段，请改为 MAIL|APPPWD|COOKIE")
    else:
        # 纯 cookie 旧格式
        mail = stem if "@" in stem else ""
        app_password = ""
        cookies = line
        format_errors.append("旧格式，请改为 MAIL|APPPWD|COOKIE")

    if not mail:
        mail = stem if "@" in stem else ""
    if not mail:
        format_errors.append("无法确定主邮箱")

    # 文件名优先作为账户标识；内容 MAIL 用于收发信
    name = stem if "@" in stem else (mail or stem)
    if "@" in stem and mail and stem.lower() != mail.lower():
        format_errors.append(f"文件名与MAIL不一致: {stem} vs {mail}")

    keys = parse_cookie_keys(cookies) if cookies else []
    missing = missing_required_keys(cookies) if cookies else list(REQUIRED_COOKIE_KEYS)

    return [
        Account(
            name=name,
            mail=mail,
            app_password=app_password,
            cookies=cookies,
            source=str(path),
            keys=keys,
            missing=missing,
            format_errors=format_errors,
        )
    ]


def load_all_accounts(accounts_files: str, base_dir: Path) -> tuple[list[Path], list[Account]]:
    files = resolve_account_files(accounts_files, base_dir)
    accounts: list[Account] = []
    for f in files:
        accounts.extend(parse_accounts_file(f))
    return files, accounts


def find_account(accounts: list[Account], name: str) -> Account | None:
    q = name.strip().lower()
    for a in accounts:
        if a.name.lower() == q or a.mail.lower() == q:
            return a
        if Path(a.source).stem.lower() == q:
            return a
        # 支持不带域名的前缀匹配
        if a.mail.lower().startswith(q + "@") or a.name.lower().startswith(q + "@"):
            return a
    return None


__all__ = [
    "Account",
    "OPTIONAL_COOKIE_KEYS",
    "REQUIRED_COOKIE_KEYS",
    "find_account",
    "load_all_accounts",
    "parse_accounts_file",
    "resolve_account_files",
]
