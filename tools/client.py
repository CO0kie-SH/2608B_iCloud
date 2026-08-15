from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

import requests

from .config import Settings


class ICloudError(RuntimeError):
    pass


class CookieInvalidError(RuntimeError):
    """账户已被标记为 cookie 失效，禁止再走生产。"""

    code = "COOKIE_INVALID"

    def __init__(self, account: str, reason: str = "", marked_at: int = 0) -> None:
        self.account = account
        self.reason = reason or "cookie_invalid"
        self.marked_at = int(marked_at or 0)
        extra = f" reason={self.reason}" if self.reason else ""
        when = f" marked_at={self.marked_at}" if self.marked_at else ""
        super().__init__(
            f"{self.code}: account={account} cookie 已失效，已移出生产线。"
            f"请执行 python main.py cookie-login -a {account} 重新采集"
            f"{extra}{when}"
        )


def is_cookie_failure(exc: BaseException | str) -> bool:
    if isinstance(exc, CookieInvalidError):
        return True
    msg = str(exc)
    return "421" in msg or "Cookie 已失效" in msg or "COOKIE_INVALID" in msg


class ICloudHMEClient:
    """iCloud Hide My Email HTTP 客户端（Cookie 会话）。"""

    def __init__(self, settings: Settings, cookies: str, timeout: float = 30.0) -> None:
        self.settings = settings
        self.timeout = timeout
        self._api_base: str | None = None

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "text/plain",
                "Origin": settings.origin,
                "Referer": f"{settings.origin}/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Cookie": cookies,
            }
        )

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> ICloudHMEClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _client_query(self) -> str:
        return urlencode(
            {
                "clientBuildNumber": self.settings.client_build,
                "clientId": self.settings.client_id,
            }
        )

    def _request(self, method: str, url: str, payload: dict | None = None) -> Any:
        data = json.dumps(payload) if payload is not None else None
        try:
            resp = self.session.request(
                method=method.upper(),
                url=url,
                data=data,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise ICloudError(f"网络请求失败: {e}") from e

        if resp.status_code < 200 or resp.status_code >= 300:
            body = resp.text[:300].replace("\n", " ")
            if resp.status_code == 421:
                raise ICloudError(
                    "HTTP 421: Cookie 已失效，请执行 "
                    "python main.py cookie-login -a ACCOUNT 重新采集"
                )
            raise ICloudError(f"HTTP {resp.status_code}: {body}")

        if not resp.text:
            return {}
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def validate_and_get_api_base(self, force: bool = False) -> str:
        if self._api_base and not force:
            return self._api_base

        url = f"{self.settings.setup_host}/setup/ws/1/validate?{self._client_query()}"
        data = self._request("POST", url)
        if not isinstance(data, dict) or "webservices" not in data:
            raise ICloudError("凭证已失效，请重新登录并更新 accounts/*.yml")

        ws = data["webservices"]
        candidates = ("maildomainws", "premiummailsettings")
        fuzzy = ("mail", "hme", "hide")

        found_url = None
        for key in candidates:
            item = ws.get(key) or {}
            if item.get("url"):
                found_url = item["url"]
                break

        if not found_url:
            for key, item in ws.items():
                if any(p in key.lower() for p in fuzzy) and isinstance(item, dict) and item.get("url"):
                    found_url = item["url"]
                    break

        if not found_url:
            raise ICloudError("该账号未开通隐藏邮件功能或无 iCloud+ 订阅")

        self._api_base = str(found_url).rstrip("/")
        return self._api_base

    def call_api(self, path: str, method: str = "GET", payload: dict | None = None) -> Any:
        base = self.validate_and_get_api_base()
        if not path.startswith("/"):
            path = "/" + path
        url = f"{base}{path}?{self._client_query()}"
        return self._request(method, url, payload)
