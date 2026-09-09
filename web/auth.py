from __future__ import annotations

"""Web 工作台账号密码登录：会话 cookie + 登录墙。"""

import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from tools.config import ensure_env_loaded

SESSION_COOKIE_NAME = "icloud_web_session"
SESSION_USER_KEY = "user"
AUTH_USERS = ("lws", "mhw")

_PBKDF2_ITERATIONS = 200_000
_LOGIN_FAIL_LIMIT = 8
_LOGIN_FAIL_WINDOW_SEC = 15 * 60


@dataclass(frozen=True)
class AuthSettings:
    session_secret: str
    disabled: bool
    cookie_secure: bool
    session_max_age: int
    password_hashes: dict[str, str]
    trust_proxy: bool
    admin_users: frozenset[str]


def _env_str(key: str, default: str = "") -> str:
    """只读进程环境 / .env，不走 proxy.yaml，避免把登录密钥写进可提交配置。"""
    ensure_env_loaded()
    raw = os.getenv(key)
    if raw is None:
        return default
    text = str(raw).strip()
    return text if text else default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env_str(key, "").lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _env_int(key: str, default: int) -> int:
    raw = _env_str(key, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _hash_password(password: str, *, salt: bytes | None = None) -> str:
    raw_salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        raw_salt,
        _PBKDF2_ITERATIONS,
    )
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${raw_salt.hex()}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iter_s, salt_hex, digest_hex = encoded.split("$", 3)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    try:
        iterations = int(iter_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual, expected)


def load_auth_settings() -> AuthSettings:
    secret = _env_str("AUTH_SESSION_SECRET")
    disabled = _env_bool("AUTH_DISABLED", False)
    hashes: dict[str, str] = {}
    for user in AUTH_USERS:
        env_key = f"AUTH_PASSWORD_{user.upper()}"
        plain = _env_str(env_key)
        if plain:
            hashes[user] = _hash_password(plain)
    return AuthSettings(
        session_secret=secret,
        disabled=disabled,
        cookie_secure=_env_bool("AUTH_COOKIE_SECURE", False),
        session_max_age=max(60, _env_int("AUTH_SESSION_MAX_AGE", 7 * 24 * 3600)),
        password_hashes=hashes,
        trust_proxy=_env_bool("AUTH_TRUST_PROXY", False),
        admin_users=frozenset(
            item.strip().lower()
            for item in _env_str("AUTH_ADMIN_USERS", "lws").split(",")
            if item.strip()
        ),
    )


def is_admin_user(user: str | None, auth_settings: AuthSettings | None = None) -> bool:
    """服务端角色判断；默认 lws 管理员，角色不由客户端 cookie 控制。"""
    if auth_settings is None:
        auth_settings = load_auth_settings()
    return bool(user and user.strip().lower() in auth_settings.admin_users)


def current_user_is_admin(request: Request, auth_settings: AuthSettings | None = None) -> bool:
    return is_admin_user(current_user(request), auth_settings)


class LoginRateLimiter:
    """同 IP 失败次数滑动窗口限流。"""

    def __init__(
        self,
        *,
        limit: int = _LOGIN_FAIL_LIMIT,
        window_sec: int = _LOGIN_FAIL_WINDOW_SEC,
    ) -> None:
        self.limit = limit
        self.window_sec = window_sec
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, ip: str, now: float) -> deque[float]:
        q = self._hits[ip]
        cutoff = now - self.window_sec
        while q and q[0] < cutoff:
            q.popleft()
        if not q and ip in self._hits:
            # 空队列可留着，避免频繁建对象；定期清也行
            pass
        return q

    def is_blocked(self, ip: str) -> bool:
        now = time.monotonic()
        q = self._prune(ip, now)
        return len(q) >= self.limit

    def record_failure(self, ip: str) -> None:
        now = time.monotonic()
        q = self._prune(ip, now)
        q.append(now)

    def clear(self, ip: str) -> None:
        self._hits.pop(ip, None)


_rate_limiter = LoginRateLimiter()


def reset_login_rate_limiter() -> None:
    """测试用：清空限流状态。"""
    global _rate_limiter
    _rate_limiter = LoginRateLimiter()


def client_ip(request: Request, *, trust_proxy: bool = False) -> str:
    if trust_proxy:
        forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if forwarded:
            return forwarded
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def current_user(request: Request) -> str | None:
    try:
        user = request.session.get(SESSION_USER_KEY)
    except AssertionError:
        # SessionMiddleware 未挂载时
        return None
    if isinstance(user, str):
        text = user.strip()
        return text or None
    return None


def safe_next_path(raw: str | None, default: str = "/") -> str:
    """只允许站内相对路径，防开放重定向。"""
    value = (raw or "").strip() or default
    if not value.startswith("/"):
        return default
    if value.startswith("//"):
        return default
    lowered = value.lower()
    if lowered.startswith("/\\") or "://" in value or "\\" in value:
        return default
    if any(ch in value for ch in ("\n", "\r", "\0")):
        return default
    return value


def unauthorized_json(message: str = "未登录或会话已过期") -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"error": {"code": "unauthorized", "message": message}},
    )


def _is_public_path(path: str, method: str) -> bool:
    if path.startswith("/static/"):
        return True
    if path == "/api/health" and method == "GET":
        return True
    if path == "/api/v1/code" and method == "GET":
        return True
    if path == "/login" and method in {"GET", "POST"}:
        return True
    if path == "/logout" and method in {"GET", "POST"}:
        return True
    return False


class AuthGateMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, auth_settings: AuthSettings) -> None:
        super().__init__(app)
        self.auth_settings = auth_settings

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if self.auth_settings.disabled:
            return await call_next(request)

        path = request.url.path
        method = request.method.upper()
        if _is_public_path(path, method):
            return await call_next(request)

        user = current_user(request)
        if user:
            return await call_next(request)

        if path.startswith("/api/"):
            return unauthorized_json()

        nxt = safe_next_path(str(request.url.path))
        if request.url.query:
            nxt = safe_next_path(f"{request.url.path}?{request.url.query}")
        return RedirectResponse(
            url=f"/login?next={quote(nxt, safe='/:?&=%')}",
            status_code=302,
        )


def create_auth_router(
    *,
    auth_settings: AuthSettings,
    templates: Any,
    app_name: str,
) -> APIRouter:
    router = APIRouter(tags=["auth"])

    def _login_page(
        request: Request,
        *,
        error: str = "",
        next_path: str = "/",
        status_code: int = 200,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "app_name": app_name,
                "error": error,
                "next_path": next_path,
            },
            status_code=status_code,
        )

    @router.get("/login", response_class=HTMLResponse)
    def login_get(request: Request, next: str = "/") -> Response:
        next_path = safe_next_path(next)
        if current_user(request) or auth_settings.disabled:
            return RedirectResponse(url=next_path, status_code=302)
        return _login_page(request, next_path=next_path)

    @router.post("/login")
    async def login_post(
        request: Request,
        username: str = Form(default=""),
        password: str = Form(default=""),
        next: str = Form(default="/"),
    ) -> Response:
        next_path = safe_next_path(next)
        if auth_settings.disabled:
            return RedirectResponse(url=next_path, status_code=302)

        ip = client_ip(request, trust_proxy=auth_settings.trust_proxy)
        if _rate_limiter.is_blocked(ip):
            return _login_page(
                request,
                error="尝试次数过多，请稍后再试",
                next_path=next_path,
                status_code=429,
            )

        user = (username or "").strip().lower()
        pwd = password or ""
        encoded = auth_settings.password_hashes.get(user, "")
        ok = bool(encoded) and _verify_password(pwd, encoded)
        if not ok:
            _rate_limiter.record_failure(ip)
            return _login_page(
                request,
                error="用户名或密码错误",
                next_path=next_path,
                status_code=401,
            )

        _rate_limiter.clear(ip)
        # 规范化用户名大小写
        canon = user if user in auth_settings.password_hashes else user
        for name in AUTH_USERS:
            if name == user:
                canon = name
                break
        request.session[SESSION_USER_KEY] = canon
        return RedirectResponse(url=next_path, status_code=302)

    def _logout(request: Request) -> Response:
        try:
            request.session.clear()
        except AssertionError:
            pass
        return RedirectResponse(url="/login", status_code=302)

    @router.get("/logout")
    def logout_get(request: Request) -> Response:
        return _logout(request)

    @router.post("/logout")
    def logout_post(request: Request) -> Response:
        return _logout(request)

    @router.get("/api/auth/me", response_model=None)
    def auth_me(request: Request) -> Response:
        if auth_settings.disabled:
            return JSONResponse({"user": current_user(request) or "disabled"})
        user = current_user(request)
        if not user:
            # 正常会被 AuthGate 拦；兜底
            return unauthorized_json()
        return JSONResponse({"user": user})

    return router
