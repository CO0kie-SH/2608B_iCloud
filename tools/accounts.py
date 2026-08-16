from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .config import env_tuple, resolve_icloud_domain
from .cookies import OPTIONAL_COOKIE_KEYS, REQUIRED_COOKIE_KEYS, missing_required_keys, parse_cookie_keys

_YAML_SUFFIXES = {".yml", ".yaml"}

# 已知邮件 provider 块名 → 人类可读标签（后续加 qqmail/gmail 等只扩这里）
PROVIDER_LABELS: dict[str, str] = {
    "apple": "Apple/iCloud",
    "163mail": "163",
    "outlook": "Microsoft Outlook",
}

# provider 块名 → 该服务商负责的邮箱域名。
# apple 块的登录名取自 appleid，而 appleid 完全可以是第三方地址（例如 163）。
# 这种账户里 apple 与 163mail 两块的 mail 相同，必须按域名判断该用谁的服务器，
# 否则会拿 163 地址去 Apple 的 IMAP 认证，一定报 AUTHENTICATIONFAILED。
_PROVIDER_DOMAIN_DEFAULTS: dict[str, tuple[str, ...]] = {
    "apple": ("icloud.com", "me.com", "mac.com"),
    "163mail": ("163.com", "126.com", "yeah.net", "vip.163.com"),
    "outlook": ("outlook.com", "hotmail.com", "live.com", "msn.com"),
}

_PROVIDER_DOMAIN_ENV_KEYS: dict[str, str] = {
    "apple": "MAIL_APPLE_DOMAINS",
    "163mail": "MAIL_163_DOMAINS",
    "outlook": "MAIL_OUTLOOK_DOMAINS",
}


@lru_cache(maxsize=1)
def provider_domains() -> dict[str, tuple[str, ...]]:
    """provider → 域名列表（可在 .env 覆盖，逗号分隔）。"""
    return {
        name: env_tuple(_PROVIDER_DOMAIN_ENV_KEYS[name], defaults)
        for name, defaults in _PROVIDER_DOMAIN_DEFAULTS.items()
    }


def provider_for_domain(mail: str) -> str:
    """邮箱地址 → 应当使用的 provider 块名；未知域名返回空串。"""
    domain = (mail or "").strip().lower().rpartition("@")[2]
    if not domain:
        return ""
    for name, domains in provider_domains().items():
        if domain in domains:
            return name
    return ""


def _retarget_by_domain(endpoint: MailProvider) -> MailProvider:
    """
    按地址域名纠正端点的 provider 名。

    下游用 provider 名挑 IMAP/SMTP 服务器，而块名不一定对应地址所属服务商
    （apple 块的登录名可能是 163 地址）。名字对不上就会连错服务器。
    域名未知时保持原样，交由默认配置兜底。
    """
    expected = provider_for_domain(endpoint.mail)
    if not expected or expected == endpoint.name:
        return endpoint
    return MailProvider(
        name=expected,
        mail=endpoint.mail,
        password=endpoint.password,
        extra=endpoint.extra,
    )


@dataclass
class MailProvider:
    """某一邮箱服务商在账户文件中的配置块。"""

    name: str
    mail: str = ""
    password: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        if self.name == "outlook":
            return bool(self.mail and self.extra.get("token_file"))
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

    @property
    def icloud_domain(self) -> str:
        """该账户 HME Cookie 所属的 iCloud Web 域名。"""
        apple = self.providers.get("apple")
        if not apple:
            return ""
        return (apple.extra.get("icloud_domain") or "").strip().lower()

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
            # apple.appleid 常与 163mail.mail 相同，两块都 ready 时按字典顺序会误选
            # apple；先按地址域名匹配服务商，再看密码是否齐全。
            expected = provider_for_domain(target)
            ranked = sorted(matches, key=lambda p: (p.name != expected, not p.ready))
            for p in ranked:
                if p.ready:
                    return p
            if ranked:
                return ranked[0]
            # 163 等密码型 provider 缺授权时不套用 Apple 密码。Outlook 保留
            # 历史兼容：只有发现同名 TXT 才切 Graph，否则仍回退 Apple IMAP。
            if expected and expected not in {"apple", "outlook"}:
                return None
            # Apple 地址可继续兼容扁平 app_password 配置。
            if expected == "apple" and self.app_password:
                return MailProvider(
                    name="apple",
                    mail=self.inbox_mail,
                    password=self.app_password,
                )
            # 未登记的第三方转发地址只作为元数据；收件回退到可用的 Apple 邮箱。

        apple = self.providers.get("apple")
        if apple and apple.ready:
            return _retarget_by_domain(apple)
        if apple and self.app_password:
            # apple 块只有 cookie、密码在扁平字段
            mail = apple.mail or self.apple_id or self.mail
            return _retarget_by_domain(
                MailProvider(name="apple", mail=mail, password=self.app_password)
            )

        for p in self.providers.values():
            if p.ready:
                return _retarget_by_domain(p)

        if self.mail and self.app_password:
            return _retarget_by_domain(
                MailProvider(name="apple", mail=self.mail, password=self.app_password)
            )
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


def create_account_file(
    accounts_files: str,
    base_dir: Path,
    account: str,
    *,
    domain: str,
) -> tuple[Path, bool]:
    """在账户目录中创建 cookie-login 所需的最小 YAML。"""
    name = (account or "").strip().lower()
    local, separator, mail_domain = name.rpartition("@")
    invalid_path_chars = set('<>:"/\\|?*')
    if (
        name.count("@") != 1
        or separator != "@"
        or not local
        or not mail_domain
        or "." not in mail_domain
        or local.startswith(".")
        or local.endswith(".")
        or ".." in name
        or mail_domain.startswith(".")
        or mail_domain.endswith(".")
        or any(ch.isspace() or ch in invalid_path_chars for ch in name)
    ):
        raise ValueError(f"账户必须是有效邮箱地址: {account}")

    account_dir: Path | None = None
    for part in re.split(r"[,;]+", accounts_files or ""):
        raw_path = part.strip().strip('"').strip("'")
        if not raw_path:
            continue
        path = Path(raw_path)
        if not path.is_absolute():
            path = base_dir / path
        if path.is_dir() or path.suffix.lower() not in _YAML_SUFFIXES:
            account_dir = path
            break

    if account_dir is None:
        raise ValueError(
            "ACCOUNTS_FILES 当前只配置了单个 YAML，请改为账户目录后再增加账号"
        )

    account_dir.mkdir(parents=True, exist_ok=True)
    target = account_dir / f"{name}.yaml"
    if target.exists():
        return target, False

    data = {
        "mail": name,
        "apple": {
            "domain": resolve_icloud_domain(domain=domain),
            "appleid": name,
            "app_password": "",
            "cookie": "",
        },
        "inbox": {"mail": name},
    }
    dumped = yaml.safe_dump(
        data,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=10_000,
    )
    try:
        with target.open("x", encoding="utf-8", newline="\n") as fh:
            fh.write(dumped)
    except FileExistsError:
        return target, False
    return target, True


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
    # Apple ID 是网页登录身份，不等于 iCloud IMAP 用户名。
    # apple.mail 可显式覆盖；缺省值稍后由根级 mail 补齐。
    mail = _as_str(raw.get("mail"))
    extra = {"appleid": apple_id} if apple_id else {}
    domain = _as_str(raw.get("domain") or raw.get("icloud_domain") or raw.get("region"))
    if domain:
        extra["icloud_domain"] = resolve_icloud_domain(domain=domain)
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
        if not apple_prov.mail and root_mail:
            apple_prov = MailProvider(
                name="apple",
                mail=root_mail,
                password=apple_prov.password,
                extra=apple_prov.extra,
            )
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


def _find_outlook_token_file(directory: Path, mail: str) -> Path | None:
    """按 inbox.mail 匹配同目录 TXT 授权文件，Windows/大小写均兼容。"""
    target = (mail or "").strip().lower()
    if not target or provider_for_domain(target) != "outlook":
        return None
    exact = directory / f"{target}.txt"
    if exact.is_file():
        return exact.resolve()
    for path in directory.glob("*.txt"):
        if path.stem.strip().lower() == target:
            return path.resolve()
    return None


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

    # Outlook 使用 OAuth refresh token；YAML 只声明 inbox.mail，授权文件沿用
    # <邮箱>.txt 的四列格式并保持独立，避免把 token 复制到 YAML。
    if inbox_mail and "outlook" not in providers:
        token_file = _find_outlook_token_file(path.parent, inbox_mail)
        if token_file:
            providers["outlook"] = MailProvider(
                name="outlook",
                mail=inbox_mail.strip().lower(),
                extra={"token_file": str(token_file)},
            )

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
