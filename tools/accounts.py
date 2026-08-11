from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .cookies import OPTIONAL_COOKIE_KEYS, REQUIRED_COOKIE_KEYS, missing_required_keys, parse_cookie_keys

_YAML_SUFFIXES = {".yml", ".yaml"}

# 已知邮件 provider 块名 → 人类可读标签（后续加 qqmail/gmail 等只扩这里）
PROVIDER_LABELS: dict[str, str] = {
    "apple": "Apple/iCloud",
    "163mail": "163",
}


@dataclass
class MailProvider:
    """某一邮箱服务商在账户文件中的配置块。"""

    name: str
    mail: str = ""
    password: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return bool(self.mail and self.password)


@dataclass
class Account:
    name: str
    mail: str
    app_password: str
    cookies: str
    source: str
    apple_id: str = ""
    inbox_mail: str = ""
    providers: dict[str, MailProvider] = field(default_factory=dict)
    keys: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    format_errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """HME 可用：cookie 完整且无格式错误。"""
        return not self.missing and not self.format_errors and bool(self.cookies)

    @property
    def mail_ready(self) -> bool:
        """当前默认收件凭证是否齐全（见 resolve_inbox）。"""
        ep = self.resolve_inbox()
        return ep is not None and ep.ready

    def get_provider(self, name: str) -> MailProvider | None:
        return self.providers.get(name)

    def resolve_inbox(self) -> MailProvider | None:
        """
        解析默认收件端点：
        1) inbox.mail 命中某 provider.mail（多命中时优先 ready）
        2) 否则优先 apple（有密码）
        3) 否则任意 ready 的 provider
        """
        target = (self.inbox_mail or "").strip().lower()
        if target:
            matches = [
                p
                for p in self.providers.values()
                if (p.mail or "").strip().lower() == target
            ]
            # apple.appleid 常与 163mail.mail 相同，优先有密码的块
            for p in matches:
                if p.ready:
                    return p
            if matches:
                return matches[0]
            # inbox 写了地址但未匹配到块：用该地址 + apple 密码兜底（扁平/旧习惯）
            if self.app_password:
                return MailProvider(name="inbox", mail=self.inbox_mail, password=self.app_password)

        apple = self.providers.get("apple")
        if apple and apple.ready:
            return apple
        if apple and self.app_password:
            # apple 块只有 cookie、密码在扁平字段
            mail = apple.mail or self.apple_id or self.mail
            return MailProvider(name="apple", mail=mail, password=self.app_password)

        for p in self.providers.values():
            if p.ready:
                return p

        if self.mail and self.app_password:
            return MailProvider(name="apple", mail=self.mail, password=self.app_password)
        return None

    def summary(self) -> str:
        parts: list[str] = []
        if self.format_errors:
            parts.append(f"格式: {', '.join(self.format_errors)}")
        if self.missing:
            parts.append(f"cookie缺: {', '.join(self.missing)}")
        if not self.cookies:
            parts.append("无cookie")
        status = "OK" if self.ok else "; ".join(parts) if parts else "ERR"
        prov = ",".join(sorted(self.providers.keys())) or "-"
        inbox = self.resolve_inbox()
        inbox_s = f"{inbox.name}:{inbox.mail}" if inbox and inbox.mail else "-"
        pwd = "pwd=Y" if self.mail_ready else "pwd=N"
        return (
            f"{self.name} <{self.mail or '-'}> [{status}] {pwd} "
            f"providers={prov} inbox={inbox_s} "
            f"keys={len(self.keys)} <- {Path(self.source).name}"
        )


def resolve_account_files(raw: str, base_dir: Path) -> list[Path]:
    """解析 ACCOUNTS_FILES：单文件 / 多文件 / 目录（*.yml / *.yaml）。"""
    paths: list[Path] = []
    if not raw or not raw.strip():
        return paths

    seen: set[Path] = set()
    for part in re.split(r"[,;]+", raw):
        part = part.strip().strip('"').strip("'")
        if not part:
            continue
        p = Path(part)
        if not p.is_absolute():
            p = base_dir / p

        if p.is_dir():
            found: list[Path] = []
            for pattern in ("*.yml", "*.yaml"):
                found.extend(p.glob(pattern))
            for f in sorted(found, key=lambda x: x.name.lower()):
                if f.name.endswith(".example") or f.name.startswith("."):
                    continue
                key = f.resolve() if f.exists() else f
                if key in seen:
                    continue
                seen.add(key)
                paths.append(f)
        else:
            key = p.resolve() if p.exists() else p
            if key in seen:
                continue
            seen.add(key)
            paths.append(p)
    return paths


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value).strip()
    return str(value).strip()


def _as_map(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    return None


def _account_with_errors(
    *,
    path: Path,
    stem: str,
    mail: str = "",
    app_password: str = "",
    cookies: str = "",
    apple_id: str = "",
    inbox_mail: str = "",
    providers: dict[str, MailProvider] | None = None,
    format_errors: list[str],
) -> Account:
    if not mail:
        mail = stem if "@" in stem else ""
    name = stem if "@" in stem else (mail or stem)
    keys = parse_cookie_keys(cookies) if cookies else []
    missing = missing_required_keys(cookies) if cookies else list(REQUIRED_COOKIE_KEYS)
    return Account(
        name=name,
        mail=mail,
        app_password=app_password,
        cookies=cookies,
        source=str(path),
        apple_id=apple_id,
        inbox_mail=inbox_mail,
        providers=providers or {},
        keys=keys,
        missing=missing,
        format_errors=format_errors,
    )


def _parse_apple_block(raw: dict[str, Any] | None) -> tuple[MailProvider | None, list[str]]:
    errors: list[str] = []
    if raw is None:
        return None, errors
    if not isinstance(raw, dict):
        return None, ["apple 必须是映射"]

    apple_id = _as_str(raw.get("appleid") or raw.get("apple_id"))
    app_password = _as_str(raw.get("app_password"))
    cookie = _as_str(raw.get("cookie"))
    # apple 登录邮箱优先 appleid；可另写 mail
    mail = _as_str(raw.get("mail")) or apple_id
    extra = {"appleid": apple_id} if apple_id else {}
    if cookie:
        extra["cookie"] = cookie
    return (
        MailProvider(name="apple", mail=mail, password=app_password, extra=extra),
        errors,
    )


def _parse_generic_mail_block(name: str, raw: Any) -> tuple[MailProvider | None, list[str]]:
    """163mail / 后续 qqmail 等：mail + imap/password/app_password。"""
    if raw is None:
        return None, []
    if not isinstance(raw, dict):
        return None, [f"{name} 必须是映射"]

    mail = _as_str(raw.get("mail"))
    password = _as_str(
        raw.get("imap")
        or raw.get("password")
        or raw.get("app_password")
        or raw.get("auth_code")
    )
    extra: dict[str, str] = {}
    for k, v in raw.items():
        ks = str(k)
        if ks in {"mail", "imap", "password", "app_password", "auth_code"}:
            continue
        extra[ks] = _as_str(v)
    return MailProvider(name=name, mail=mail, password=password, extra=extra), []


def _extract_from_data(data: dict[str, Any]) -> tuple[
    str,
    str,
    str,
    str,
    str,
    dict[str, MailProvider],
    list[str],
]:
    """
    从 YAML 根映射抽出账户字段。
    支持：
      - 嵌套：apple / 163mail / inbox
      - 扁平缩减：mail + app_password + cookie
    """
    errors: list[str] = []
    providers: dict[str, MailProvider] = {}

    root_mail = _as_str(data.get("mail"))

    apple_raw = _as_map(data.get("apple"))
    if data.get("apple") is not None and apple_raw is None:
        errors.append("apple 必须是映射")
    apple_prov, apple_errs = _parse_apple_block(apple_raw)
    errors.extend(apple_errs)
    if apple_prov:
        providers["apple"] = apple_prov

    # 其它 *mail 块（163mail、后续 qqmail…），排除 inbox
    reserved = {"mail", "apple", "inbox", "app_password", "cookie", "name", "enabled", "notes"}
    for key, val in data.items():
        ks = str(key)
        if ks in reserved or ks == "apple":
            continue
        if not isinstance(val, dict):
            continue
        # 约定：以 mail 结尾或已在 PROVIDER_LABELS 的块视为邮箱 provider
        if ks.endswith("mail") or ks in PROVIDER_LABELS:
            prov, perr = _parse_generic_mail_block(ks, val)
            errors.extend(perr)
            if prov:
                providers[ks] = prov

    inbox_mail = ""
    inbox_raw = data.get("inbox")
    if inbox_raw is not None:
        if isinstance(inbox_raw, str):
            inbox_mail = inbox_raw.strip()
        elif isinstance(inbox_raw, dict):
            inbox_mail = _as_str(inbox_raw.get("mail"))
        else:
            errors.append("inbox 必须是映射或邮箱字符串")

    # 扁平缩减：根级 app_password / cookie
    flat_password = _as_str(data.get("app_password"))
    flat_cookie = _as_str(data.get("cookie"))

    apple_id = ""
    app_password = flat_password
    cookies = flat_cookie

    if apple_prov:
        apple_id = _as_str(apple_prov.extra.get("appleid")) or apple_prov.mail or root_mail
        if apple_prov.password:
            app_password = apple_prov.password
        if apple_prov.extra.get("cookie"):
            cookies = apple_prov.extra["cookie"]
        # 扁平字段可补全 apple 块缺省
        if flat_password and not apple_prov.password:
            app_password = flat_password
            providers["apple"] = MailProvider(
                name="apple",
                mail=apple_prov.mail or apple_id or root_mail,
                password=flat_password,
                extra=apple_prov.extra,
            )
        if flat_cookie and not apple_prov.extra.get("cookie"):
            cookies = flat_cookie
            extra = dict(providers["apple"].extra)
            extra["cookie"] = flat_cookie
            providers["apple"] = MailProvider(
                name="apple",
                mail=providers["apple"].mail,
                password=providers["apple"].password,
                extra=extra,
            )
    elif flat_password or flat_cookie:
        # 纯扁平 → 合成 apple provider，保持旧文件可用
        apple_id = root_mail
        providers["apple"] = MailProvider(
            name="apple",
            mail=root_mail,
            password=flat_password,
            extra={"cookie": flat_cookie, "appleid": root_mail} if root_mail else {"cookie": flat_cookie},
        )

    return root_mail, apple_id, app_password, cookies, inbox_mail, providers, errors


def parse_accounts_file(path: Path) -> list[Account]:
    """
    YAML 账户文件（一文件一账户）。

    嵌套（推荐）::
        mail: user@icloud.com
        apple:
          appleid: ...
          app_password: ...
          cookie: "..."
        163mail:
          mail: ...
          imap: ...
        inbox:
          mail: ...

    扁平缩减（兼容）::
        mail: ...
        app_password: ...
        cookie: "..."
    """
    if not path.exists():
        return []

    stem = path.stem.strip()
    suffix = path.suffix.lower()

    if suffix not in _YAML_SUFFIXES:
        return [
            _account_with_errors(
                path=path,
                stem=stem,
                format_errors=["已改为 YAML，请使用 .yml / .yaml"],
            )
        ]

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return [
            _account_with_errors(
                path=path,
                stem=stem,
                format_errors=[f"无法读取文件: {e}"],
            )
        ]

    if not text.strip():
        return [
            _account_with_errors(
                path=path,
                stem=stem,
                format_errors=["文件为空"],
            )
        ]

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        msg = str(e).split("\n", 1)[0].strip() or "YAML 语法错误"
        return [
            _account_with_errors(
                path=path,
                stem=stem,
                format_errors=[f"YAML 语法错误: {msg}"],
            )
        ]

    if data is None:
        return [
            _account_with_errors(
                path=path,
                stem=stem,
                format_errors=["文件为空"],
            )
        ]

    if not isinstance(data, dict):
        return [
            _account_with_errors(
                path=path,
                stem=stem,
                format_errors=["YAML 根节点必须是映射"],
            )
        ]

    root_mail, apple_id, app_password, cookies, inbox_mail, providers, extract_errors = _extract_from_data(
        data
    )
    format_errors = list(extract_errors)

    mail = root_mail
    if not mail:
        mail = stem if "@" in stem else ""
    if not mail:
        format_errors.append("无法确定主邮箱（根字段 mail）")

    name = stem if "@" in stem else (mail or stem)
    if "@" in stem and mail and stem.lower() != mail.lower():
        format_errors.append(f"文件名与 mail 不一致: {stem} vs {mail}")

    # apple IMAP 用户：appleid 优先
    apple = providers.get("apple")
    if apple and not apple.mail:
        fixed_mail = apple_id or mail
        providers["apple"] = MailProvider(
            name="apple",
            mail=fixed_mail,
            password=apple.password or app_password,
            extra=apple.extra,
        )

    keys = parse_cookie_keys(cookies) if cookies else []
    missing = missing_required_keys(cookies) if cookies else list(REQUIRED_COOKIE_KEYS)

    return [
        Account(
            name=name,
            mail=mail,
            app_password=app_password,
            cookies=cookies,
            source=str(path),
            apple_id=apple_id,
            inbox_mail=inbox_mail,
            providers=providers,
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
        if a.apple_id and a.apple_id.lower() == q:
            return a
        if Path(a.source).stem.lower() == q:
            return a
        if a.mail.lower().startswith(q + "@") or a.name.lower().startswith(q + "@"):
            return a
        for p in a.providers.values():
            if p.mail and (p.mail.lower() == q or p.mail.lower().startswith(q + "@")):
                return a
    return None


__all__ = [
    "Account",
    "MailProvider",
    "OPTIONAL_COOKIE_KEYS",
    "PROVIDER_LABELS",
    "REQUIRED_COOKIE_KEYS",
    "find_account",
    "load_all_accounts",
    "parse_accounts_file",
    "resolve_account_files",
]
