from __future__ import annotations

import os
import unittest
from unittest import mock

# 必须在 import create_app / load_auth_settings 之前写入测试凭据
_AUTH_ENV = {
    "AUTH_SESSION_SECRET": "test-secret-not-for-prod",
    "AUTH_PASSWORD_LWS": "test-lws-pass",
    "AUTH_PASSWORD_MHW": "test-mhw-pass",
    "AUTH_DISABLED": "false",
    "AUTH_COOKIE_SECURE": "false",
}
for _k, _v in _AUTH_ENV.items():
    os.environ[_k] = _v

from fastapi.testclient import TestClient  # noqa: E402

from tools.config import ensure_env_loaded  # noqa: E402
from web.auth import reset_login_rate_limiter  # noqa: E402
from web.app import create_app  # noqa: E402


def _fresh_client(*, auth_disabled: bool = False) -> TestClient:
    env = dict(_AUTH_ENV)
    env["AUTH_DISABLED"] = "true" if auth_disabled else "false"
    # 清 dotenv / settings 缓存，避免被本机 .env 盖掉测试变量
    ensure_env_loaded.cache_clear()
    with mock.patch.dict(os.environ, env, clear=False):
        # 强制 os.environ 优先生效：config_value 会先读 proxy.yaml，再 getenv
        # 测试密码不在 proxy.yaml，直接 getenv 即可
        reset_login_rate_limiter()
        app = create_app()
        return TestClient(app)


class WebAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        ensure_env_loaded.cache_clear()
        for k, v in _AUTH_ENV.items():
            os.environ[k] = v
        reset_login_rate_limiter()
        self.client = _fresh_client(auth_disabled=False)

    def tearDown(self) -> None:
        reset_login_rate_limiter()

    def _login(self, username: str, password: str, *, next_path: str = "/") -> object:
        return self.client.post(
            "/login",
            data={"username": username, "password": password, "next": next_path},
            follow_redirects=False,
        )

    def test_unauthenticated_html_redirects_to_login(self) -> None:
        for path in ("/", "/mailbox"):
            res = self.client.get(path, follow_redirects=False)
            self.assertEqual(res.status_code, 302, path)
            loc = res.headers.get("location") or ""
            self.assertTrue(loc.startswith("/login"), loc)
            self.assertIn("next=", loc)

    def test_unauthenticated_api_returns_401_json(self) -> None:
        for path in ("/api/home/stats", "/api/claims/stats"):
            res = self.client.get(path)
            self.assertEqual(res.status_code, 401, path)
            body = res.json()
            self.assertEqual(body["error"]["code"], "unauthorized")

    def test_public_endpoints_skip_login_wall(self) -> None:
        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertTrue(health.json().get("ok"))

        code = self.client.get("/api/v1/code")
        # 缺 token：业务 401 missing token，不是登录墙 unauthorized
        self.assertEqual(code.status_code, 401)
        detail = code.json()
        # FastAPI HTTPException -> {"detail": "..."}
        self.assertIn("missing token", str(detail).lower())
        if isinstance(detail, dict) and "error" in detail:
            self.assertNotEqual(detail["error"].get("code"), "unauthorized")

        css = self.client.get("/static/app.css")
        self.assertEqual(css.status_code, 200)
        self.assertIn("--bg", css.text)

    def test_bad_password_rejected(self) -> None:
        res = self._login("lws", "wrong-password")
        self.assertEqual(res.status_code, 401)
        self.assertIn("用户名或密码错误", res.text)

    def test_login_lws_and_mhw_then_me(self) -> None:
        for user, pwd in (("lws", "test-lws-pass"), ("mhw", "test-mhw-pass")):
            client = _fresh_client()
            res = client.post(
                "/login",
                data={"username": user, "password": pwd, "next": "/"},
                follow_redirects=False,
            )
            self.assertEqual(res.status_code, 302, user)
            self.assertEqual(res.headers.get("location"), "/")

            home = client.get("/")
            self.assertEqual(home.status_code, 200)
            self.assertIn(user, home.text)

            me = client.get("/api/auth/me")
            self.assertEqual(me.status_code, 200)
            self.assertEqual(me.json(), {"user": user})

    def test_open_redirect_rejected(self) -> None:
        res = self._login(
            "lws",
            "test-lws-pass",
            next_path="https://evil.example",
        )
        self.assertEqual(res.status_code, 302)
        loc = res.headers.get("location") or ""
        self.assertEqual(loc, "/")
        self.assertNotIn("evil.example", loc)

    def test_logout_blocks_protected_pages(self) -> None:
        self.assertEqual(self._login("lws", "test-lws-pass").status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)

        out = self.client.get("/logout", follow_redirects=False)
        self.assertEqual(out.status_code, 302)
        self.assertEqual(out.headers.get("location"), "/login")

        blocked = self.client.get("/", follow_redirects=False)
        self.assertEqual(blocked.status_code, 302)
        self.assertTrue((blocked.headers.get("location") or "").startswith("/login"))

        api = self.client.get("/api/home/stats")
        self.assertEqual(api.status_code, 401)

    def test_auth_disabled_allows_anonymous(self) -> None:
        client = _fresh_client(auth_disabled=True)
        res = client.get("/")
        self.assertEqual(res.status_code, 200)
        stats = client.get("/api/home/stats")
        self.assertEqual(stats.status_code, 200)


if __name__ == "__main__":
    unittest.main()
