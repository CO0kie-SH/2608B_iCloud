from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlencode

import requests

from .config import Settings, ensure_env_loaded
from .cookies import normalize_hme_cookie_header


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
    """iCloud Hide My Email HTTP 客户端（直连优先，传输失败时代理兜底）。"""

    def __init__(self, settings: Settings, cookies: str, timeout: float = 30.0) -> None:
        self.settings = settings
        self.timeout = timeout
        self._api_base: str | None = None
        self._validation_data: dict[str, Any] | None = None
        self.cookies = normalize_hme_cookie_header(cookies)
        # 仅记录链路类型、结果和 HTTP 状态，不记录代理 URL 或认证信息。
        self._network_attempts: list[dict[str, Any]] = []

        self.session = self._new_session()
        self._proxy_url = self._resolve_proxy()
        self._proxy_session = self._new_session(self._proxy_url) if self._proxy_url else None

    def _new_session(self, proxy_url: str | None = None) -> requests.Session:
        session = requests.Session()
        # 两条链路都显式关闭 trust_env，避免环境变量在直连/代理之间串线。
        session.trust_env = False
        if proxy_url:
            session.proxies.update({"http": proxy_url, "https": proxy_url})
        session.headers.update(
            {
                "Content-Type": "text/plain",
                "Origin": self.settings.origin,
                "Referer": f"{self.settings.origin}/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Cookie": self.cookies,
            }
        )
        return session

    def _resolve_proxy(self) -> str | None:
        """解析 HME 兜底代理；支持 HTTP(S) 与 socks5/socks5h。"""
        ensure_env_loaded()
        configured = str(getattr(self.settings, "hme_proxy", "") or "").strip()
        if configured:
            candidates = [configured]
        else:
            candidates = [
                os.getenv("HTTPS_PROXY", ""),
                os.getenv("https_proxy", ""),
                os.getenv("HTTP_PROXY", ""),
                os.getenv("http_proxy", ""),
                os.getenv("ALL_PROXY", ""),
                os.getenv("all_proxy", ""),
                str(getattr(self.settings, "camoufox_proxy", "") or ""),
            ]
        for raw in candidates:
            value = str(raw or "").strip()
            if not value:
                continue
            if value.lower() in {"none", "off", "direct"}:
                return None
            return value
        return None

    def close(self) -> None:
        self.session.close()
        if self._proxy_session is not None:
            self._proxy_session.close()

    @property
    def network_attempts(self) -> list[dict[str, Any]]:
        """返回本客户端已发生的网络尝试（副本）。"""
        return [dict(item) for item in self._network_attempts]

    def network_report(self, *, success: bool | None = None) -> dict[str, Any]:
        """生成可安全展示在生产任务中的链路报告。"""
        routes = [str(item.get("route") or "") for item in self._network_attempts]
        direct_attempted = "direct" in routes
        proxy_attempted = "proxy" in routes
        if direct_attempted and proxy_attempted:
            route = "direct_then_proxy"
        elif proxy_attempted:
            route = "proxy"
        elif direct_attempted:
            route = "direct"
        else:
            route = "not_started"
        successful_attempts = sum(1 for item in self._network_attempts if item.get("ok"))
        failed_attempts = sum(1 for item in self._network_attempts if not item.get("ok"))
        return {
            "route": route,
            "used_proxy": proxy_attempted,
            "direct_attempted": direct_attempted,
            "proxy_attempted": proxy_attempted,
            "successful_attempts": successful_attempts,
            "failed_attempts": failed_attempts,
            "success": success,
            "status": (
                "success" if success is True else "failed" if success is False else "unknown"
            ),
        }

    def _safe_transport_error(self, route: str, exc: BaseException) -> str:
        detail = str(exc)
        if self._proxy_url:
            detail = detail.replace(self._proxy_url, "<proxy>")
        return f"{route}: {type(exc).__name__}: {detail[:240]}"

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
        transport_errors: list[str] = []
        sessions = [("direct", self.session)]
        if self._proxy_session is not None:
            sessions.append(("proxy", self._proxy_session))
        for route, session in sessions:
            try:
                resp = session.request(
                    method=method.upper(),
                    url=url,
                    data=data,
                    timeout=self.timeout,
                )
                self._network_attempts.append(
                    {
                        "route": route,
                        "ok": 200 <= int(resp.status_code) < 300,
                        "http_status": int(resp.status_code),
                    }
                )
                break
            except requests.RequestException as exc:
                self._network_attempts.append(
                    {
                        "route": route,
                        "ok": False,
                        "error_type": type(exc).__name__,
                    }
                )
                transport_errors.append(self._safe_transport_error(route, exc))
                if route == "direct" and self._proxy_session is not None:
                    print(
                        f"[hme-http] direct transport failed ({type(exc).__name__}); "
                        "retrying via proxy"
                    )
        else:
            detail = "; ".join(transport_errors) or "无可用连接方式"
            raise ICloudError(f"网络请求失败: {detail}") from None

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
        self._validation_data = data

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

    def get_account_apple_id(self) -> str:
        """返回校验响应中的主 Apple ID，而不是 iCloud 邮箱别名。"""
        if self._validation_data is None:
            self.validate_and_get_api_base()
        data = self._validation_data or {}
        ds_info = data.get("dsInfo")
        if not isinstance(ds_info, dict):
            return ""
        for key in ("appleId", "primaryEmail"):
            value = str(ds_info.get(key) or "").strip().lower()
            if value:
                return value
        return ""

    def call_api(self, path: str, method: str = "GET", payload: dict | None = None) -> Any:
        base = self.validate_and_get_api_base()
        if not path.startswith("/"):
            path = "/" + path
        url = f"{base}{path}?{self._client_query()}"
        return self._request(method, url, payload)
