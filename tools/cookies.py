from __future__ import annotations

import re
from http.cookiejar import Cookie
from http.cookiejar import CookieJar

REQUIRED_COOKIE_KEYS = (
    "X-APPLE-WEBAUTH-TOKEN",
    "X-APPLE-WEBAUTH-USER",
    "X-APPLE-DS-WEB-SESSION-TOKEN",
    "X-APPLE-WEBAUTH-LOGIN",
)

OPTIONAL_COOKIE_KEYS = (
    "X-APPLE-WEBAUTH-HSA-TRUST",
    "X-APPLE-UNIQUE-CLIENT-ID",
    "X-APPLE-WEB-ID",
    "X-APPLE-WEBAUTH-VALIDATE",
)


def parse_cookie_keys(cookies: str) -> list[str]:
    return re.findall(r"(?:^|;\s*)([A-Za-z0-9_.-]+)=", cookies)


def normalize_cookie_value(value: object) -> str:
    """规范 Cookie 值，兼容 YAML/浏览器导出时重复包裹的引号。"""
    result = str(value or "").strip()
    while len(result) >= 2 and result[0] == result[-1] and result[0] in "\"'":
        result = result[1:-1]
    return result


def parse_cookie_header(cookies: str) -> dict[str, str]:
    """把 Cookie 请求头字符串解析为 dict。"""
    result: dict[str, str] = {}
    if not cookies:
        return result

    for part in cookies.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = normalize_cookie_value(value)
        result[key] = value
    return result


def cookie_header_from_dict(data: dict[str, str]) -> str:
    return "; ".join(f"{k}={normalize_cookie_value(v)}" for k, v in data.items())


def normalize_cookie_header(cookies: str) -> str:
    """将任意 Cookie 请求头解析并重建为标准的无引号格式。"""
    return cookie_header_from_dict(parse_cookie_header(cookies))


def normalize_hme_cookie_header(cookies: str) -> str:
    """仅保留 iCloud HME 接口需要的 Apple Web 会话 Cookie。"""
    parsed = parse_cookie_header(cookies)
    selected = {
        key: value
        for key, value in parsed.items()
        if key.upper().startswith(("X-APPLE-", "X_APPLE_"))
    }
    return cookie_header_from_dict(selected)


def missing_required_keys(cookies: str) -> list[str]:
    keys = set(parse_cookie_keys(cookies))
    return [k for k in REQUIRED_COOKIE_KEYS if k not in keys]


def build_cookie_jar(cookies: str, domain: str) -> CookieJar:
    jar = CookieJar()
    parsed = parse_cookie_header(cookies)
    host = domain.lstrip(".")
    for name, value in parsed.items():
        cookie = Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=host,
            domain_specified=True,
            domain_initial_dot=False,
            path="/",
            path_specified=True,
            secure=True,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
        jar.set_cookie(cookie)
    return jar
