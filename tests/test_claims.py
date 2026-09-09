from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# 鉴权默认开启：在 import create_app 前写入测试凭据
os.environ.setdefault("AUTH_SESSION_SECRET", "test-secret-not-for-prod")
os.environ.setdefault("AUTH_PASSWORD_LWS", "test-lws-pass")
os.environ.setdefault("AUTH_PASSWORD_MHW", "test-mhw-pass")
os.environ["AUTH_DISABLED"] = "false"

from fastapi.testclient import TestClient

from tools.db import AliasDB
from web.app import create_app


class ClaimPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "aliases.db"
        self.db = AliasDB(self.db_path)
        for i in range(5):
            self.db.upsert_alias(
                account="user001@icloud.com",
                parent_mail="user001@icloud.com",
                hme=f"alias{i}@icloud.com",
                label=f"L{i}",
                is_active=True,
                source="test",
            )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_claim_reduces_available_and_records_order(self) -> None:
        order = self.db.claim_aliases(
            count=2,
            contact_email="me@example.com",
            note="自用测试",
        )
        self.assertEqual(order["count"], 2)
        self.assertEqual(len(order["emails"]), 2)
        self.assertTrue(order["order_no"].startswith("CLM"))
        stats = self.db.get_claim_stats()
        self.assertEqual(stats["total"], 5)
        self.assertEqual(stats["available"], 3)
        self.assertEqual(stats["claimed"], 2)
        self.assertEqual(stats["orders"], 1)

        again = self.db.claim_aliases(count=3, contact_email="me@example.com")
        self.assertEqual(again["count"], 3)
        with self.assertRaises(ValueError):
            self.db.claim_aliases(count=1, contact_email="me@example.com")

    def test_home_stats_exclude_claimed(self) -> None:
        self.db.claim_aliases(count=1, contact_email="a@b.com", note="x")
        pool = self.db.get_alias_pool_stats()
        self.assertEqual(pool["total"], 5)
        self.assertEqual(pool["available"], 4)
        self.assertEqual(pool["claimed"], 1)

    def test_claim_whitelist_and_blacklist_filter_without_config_file(self) -> None:
        for account, prefix in (("user002@icloud.com", "KEEP"), ("user003@icloud.com", "DROP")):
            for i in range(2):
                self.db.upsert_alias(
                    account=account,
                    parent_mail=account,
                    hme=f"{prefix.lower()}{i}@icloud.com",
                    label=f"{prefix}_L{i}",
                    is_active=True,
                    source="test",
                )

        order = self.db.claim_aliases(
            count=2,
            contact_email="me@example.com",
            whitelist={"accounts": ["user002@icloud.com"]},
            blacklist={"label_prefixes": ["DROP"]},
        )
        self.assertEqual(
            [item["account"] for item in order["items"]],
            ["user002@icloud.com", "user002@icloud.com"],
        )

        with self.assertRaisesRegex(ValueError, "当前仅剩 0 个"):
            self.db.claim_aliases(
                count=1,
                contact_email="me@example.com",
                whitelist={"accounts": ["user003@icloud.com"]},
                blacklist={"hmes": ["drop0@icloud.com", "drop1@icloud.com"]},
            )

    def test_saved_database_policy_is_applied_by_default(self) -> None:
        self.db.upsert_alias(account="user002@icloud.com", parent_mail="user002@icloud.com", hme="allow@icloud.com", label="OK", source="test")
        self.db.upsert_alias(account="user003@icloud.com", parent_mail="user003@icloud.com", hme="deny@icloud.com", label="OK", source="test")
        self.db.save_claim_policy(
            whitelist={"accounts": ["user002@icloud.com\nuser003@icloud.com"]},
            blacklist={"hmes": ["deny@icloud.com"]},
            updated_by="lws",
        )
        self.assertEqual(
            self.db.get_claim_policy()["whitelist"]["accounts"],
            ["user002@icloud.com", "user003@icloud.com"],
        )
        order = self.db.claim_aliases(count=1, contact_email="me@example.com")
        self.assertEqual(order["emails"], ["allow@icloud.com"])


class ClaimApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "aliases.db"
        self.db = AliasDB(self.db_path)
        for i in range(3):
            self.db.upsert_alias(
                account="user001@icloud.com",
                parent_mail="user001@icloud.com",
                hme=f"api{i}@icloud.com",
                label=f"A{i}",
                is_active=True,
                source="test",
            )
        self.sender = mock.Mock()
        self.sender.name = "user001@icloud.com"
        self.sender.mail = "user001@icloud.com"
        self.sender.mail_ready = True

        self.mail_client = mock.Mock()
        self.mail_client.mail = "user001@icloud.com"

        self.patches = [
            mock.patch("web.deps.get_db", return_value=self.db),
            mock.patch("web.routers.claims.get_db", return_value=self.db),
            mock.patch("web.app.get_db", return_value=self.db),
            mock.patch("web.routers.claims.get_accounts", return_value=[self.sender]),
            mock.patch("web.routers.claims.get_mail_client", return_value=self.mail_client),
        ]
        for p in self.patches:
            p.start()
        self.client = TestClient(create_app())
        login = self.client.post(
            "/login",
            data={
                "username": "lws",
                "password": os.environ["AUTH_PASSWORD_LWS"],
                "next": "/",
            },
            follow_redirects=False,
        )
        self.assertEqual(login.status_code, 302, login.text)

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_checkout_sends_email_and_lists_order(self) -> None:
        res = self.client.post(
            "/api/claims/checkout",
            json={
                "count": 2,
                "contact_email": "buyer@example.com",
                "note": "注册用",
                "send_email": True,
            },
        )
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["deliver_status"], "sent")
        self.assertEqual(len(body["emails"]), 2)
        self.mail_client.send.assert_called_once()
        args = self.mail_client.send.call_args[0]
        self.assertEqual(args[0], "buyer@example.com")
        self.assertIn("buyer@example.com", args[0])
        self.assertIn(body["emails"][0], args[2])

        stats = self.client.get("/api/claims/stats").json()
        self.assertEqual(stats["available"], 1)
        self.assertEqual(stats["claimed"], 2)

        orders = self.client.get("/api/claims/orders").json()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["order_no"], body["order_no"])

        page = self.client.get("/claims")
        self.assertEqual(page.status_code, 200)
        self.assertIn("领取确认", page.text)

    def test_checkout_passes_whitelist_and_blacklist_to_database(self) -> None:
        for account, hme in (
            ("user002@icloud.com", "allow@icloud.com"),
            ("user003@icloud.com", "deny@icloud.com"),
        ):
            self.db.upsert_alias(
                account=account,
                parent_mail=account,
                hme=hme,
                label="TEST",
                is_active=True,
                source="test",
            )
        res = self.client.post(
            "/api/claims/checkout",
            json={
                "count": 1,
                "contact_email": "buyer@example.com",
                "send_email": False,
                "whitelist": {"accounts": ["user002@icloud.com", "user003@icloud.com"]},
                "blacklist": {"hmes": ["deny@icloud.com"]},
            },
        )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["emails"], ["allow@icloud.com"])

    def test_checkout_rejects_non_object_policy(self) -> None:
        res = self.client.post(
            "/api/claims/checkout",
            json={"count": 1, "contact_email": "buyer@example.com", "whitelist": ["x"]},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("whitelist", res.json()["detail"])

    def test_only_admin_can_modify_global_policy(self) -> None:
        # 隔离角色配置，验证服务端权限判断，不依赖运行环境的管理员名单。
        with mock.patch("web.routers.claims.is_admin_user", side_effect=lambda user, _settings: user == "lws"):
            saved = self.client.put(
                "/api/claims/policy",
                json={"whitelist": {"accounts": ["user001@icloud.com"]}, "blacklist": {}},
            )
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(saved.json()["updated_by"], "lws")

            self.client.get("/logout")
            login = self.client.post(
                "/login",
                data={"username": "mhw", "password": os.environ["AUTH_PASSWORD_MHW"], "next": "/"},
                follow_redirects=False,
            )
            self.assertEqual(login.status_code, 302)
            visible = self.client.get("/api/claims/policy")
            self.assertEqual(visible.status_code, 200)
            forbidden = self.client.put(
                "/api/claims/policy",
                json={"whitelist": {}, "blacklist": {"hmes": ["TARGET@icloud.com"]}},
            )
            self.assertEqual(forbidden.status_code, 403)

    def test_claim_policy_page_is_available_to_authenticated_user(self) -> None:
        page = self.client.get("/claim-policy")
        self.assertEqual(page.status_code, 200)
        self.assertIn("领取黑白名单", page.text)


if __name__ == "__main__":
    unittest.main()
