from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml
import requests

from tools.accounts import parse_accounts_file
from tools.client import ICloudError, ICloudHMEClient
from tools.config import Settings, settings_for_account, settings_for_browser
from tools.cookie_capture import (
    playwright_cookies_to_header,
    update_account_apple_id,
    update_account_cookie,
    update_account_inbox,
)
from tools.cookies import (
    missing_required_keys,
    normalize_cookie_header,
    normalize_hme_cookie_header,
)


BROKEN_COOKIE = "; ".join(
    (
        'X-APPLE-WEBAUTH-TOKEN=""TOKEN""',
        'X-APPLE-WEBAUTH-USER=""USER""',
        'X-APPLE-DS-WEB-SESSION-TOKEN=""SESSION""',
        'X-APPLE-WEBAUTH-LOGIN=""LOGIN""',
    )
)
NORMALIZED_COOKIE = "; ".join(
    (
        "X-APPLE-WEBAUTH-TOKEN=TOKEN",
        "X-APPLE-WEBAUTH-USER=USER",
        "X-APPLE-DS-WEB-SESSION-TOKEN=SESSION",
        "X-APPLE-WEBAUTH-LOGIN=LOGIN",
    )
)
MIXED_COOKIE = BROKEN_COOKIE + "; aasp=APPLE_LOGIN; site=CHN"


class CookieNormalizationTests(unittest.TestCase):
    def test_repeated_outer_quotes_are_removed(self) -> None:
        normalized = normalize_cookie_header(BROKEN_COOKIE)

        self.assertEqual(normalized, NORMALIZED_COOKIE)
        self.assertEqual(missing_required_keys(normalized), [])

    def test_hme_client_uses_normalized_cookie_header(self) -> None:
        settings = Settings(
            app_name="test",
            debug=False,
            domain="icloud.com.cn",
            accounts_files="accounts/",
            client_build="BUILD",
            client_id="CLIENT",
            camoufox_dir="",
            camoufox_proxy="",
            log_dir="",
            base_dir=Path.cwd(),
        )

        with ICloudHMEClient(settings, MIXED_COOKIE) as client:
            self.assertEqual(client.cookies, NORMALIZED_COOKIE)
            self.assertEqual(client.session.headers["Cookie"], NORMALIZED_COOKIE)

    def test_hme_client_prefers_direct_and_falls_back_to_proxy_on_transport_error(self) -> None:
        settings = Settings(
            app_name="test",
            debug=False,
            domain="icloud.com",
            accounts_files="accounts/",
            client_build="BUILD",
            client_id="CLIENT",
            camoufox_dir="",
            camoufox_proxy="",
            log_dir="",
            hme_proxy="socks5h://127.0.0.1:1080",
        )
        response = Mock(status_code=200, text='{"ok": true}')
        response.json.return_value = {"ok": True}

        with ICloudHMEClient(settings, NORMALIZED_COOKIE) as client:
            self.assertFalse(client.session.trust_env)
            self.assertEqual(client.session.proxies, {})
            self.assertIsNotNone(client._proxy_session)
            self.assertEqual(
                client._proxy_session.proxies["https"], "socks5h://127.0.0.1:1080"
            )
            with patch.object(
                client.session, "request", side_effect=requests.ConnectionError("direct down")
            ) as direct, patch.object(
                client._proxy_session, "request", return_value=response
            ) as proxy:
                self.assertEqual(client._request("GET", "https://example.test"), {"ok": True})
                self.assertEqual(client.network_report(success=True)["route"], "direct_then_proxy")
                self.assertTrue(client.network_report(success=True)["used_proxy"])
                self.assertEqual(client.network_report(success=True)["successful_attempts"], 1)
                self.assertEqual(client.network_report(success=True)["failed_attempts"], 1)
            direct.assert_called_once()
            proxy.assert_called_once()

    def test_hme_client_does_not_fallback_on_http_error(self) -> None:
        settings = Settings(
            app_name="test",
            debug=False,
            domain="icloud.com",
            accounts_files="accounts/",
            client_build="BUILD",
            client_id="CLIENT",
            camoufox_dir="",
            camoufox_proxy="",
            log_dir="",
            hme_proxy="http://proxy.example:8080",
        )
        response = Mock(status_code=421, text="expired")

        with ICloudHMEClient(settings, NORMALIZED_COOKIE) as client:
            with patch.object(client.session, "request", return_value=response) as direct, patch.object(
                client._proxy_session, "request"
            ) as proxy:
                with self.assertRaisesRegex(ICloudError, "HTTP 421"):
                    client._request("GET", "https://example.test")
                report = client.network_report(success=False)
                self.assertEqual(report["route"], "direct")
                self.assertFalse(report["used_proxy"])
            direct.assert_called_once()
            proxy.assert_not_called()

    def test_hme_header_drops_unrelated_login_cookies(self) -> None:
        self.assertEqual(normalize_hme_cookie_header(MIXED_COOKIE), NORMALIZED_COOKIE)

    def test_hme_api_uses_global_setup_even_for_cn_accounts(self) -> None:
        settings = Settings(
            app_name="test",
            debug=False,
            domain="icloud.com",
            accounts_files="accounts/",
            client_build="BUILD",
            client_id="CLIENT",
            camoufox_dir="",
            camoufox_proxy="",
            log_dir="logs",
        )
        account = type("Acc", (), {"icloud_domain": "icloud.com.cn"})()

        self.assertEqual(settings_for_account(settings, account).setup_host, "https://setup.icloud.com")
        self.assertEqual(settings_for_browser(settings, account).origin, "https://www.icloud.com.cn")

    def test_playwright_cookies_follow_request_domain_scope(self) -> None:
        records = [
            {"name": "X-APPLE-WEBAUTH-TOKEN", "value": "TOKEN", "domain": ".icloud.com.cn"},
            {"name": "X-APPLE-WEBAUTH-USER", "value": "USER", "domain": ".icloud.com.cn"},
            {
                "name": "X-APPLE-DS-WEB-SESSION-TOKEN",
                "value": "SESSION",
                "domain": ".icloud.com.cn",
            },
            {"name": "X-APPLE-WEBAUTH-LOGIN", "value": "LOGIN", "domain": ".icloud.com.cn"},
            {"name": "aasp", "value": "APPLE_LOGIN", "domain": ".apple.com"},
            {"name": "site", "value": "CHN", "domain": ".idmsa.apple.com"},
            {"name": "X-APPLE-WEBAUTH-TOKEN", "value": "GLOBAL", "domain": ".icloud.com"},
        ]

        header = playwright_cookies_to_header(
            records,
            request_host="setup.icloud.com.cn",
        )

        self.assertEqual(header, NORMALIZED_COOKIE)

    def test_account_cookie_is_normalized_before_yaml_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "account.yaml"
            path.write_text("apple:\n  cookie: old\n", encoding="utf-8")

            location = update_account_cookie(path, MIXED_COOKIE, backup=False)
            saved = yaml.safe_load(path.read_text(encoding="utf-8"))

        self.assertEqual(location, "apple.cookie")
        self.assertEqual(saved["apple"]["cookie"], NORMALIZED_COOKIE)

    def test_account_inbox_is_created_without_losing_other_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "account.yaml"
            path.write_text(
                "mail: user@icloud.com\napple:\n  cookie: TOKEN\n",
                encoding="utf-8",
            )

            location = update_account_inbox(
                path,
                "Forward.Target@163.com",
                backup=False,
            )
            saved = yaml.safe_load(path.read_text(encoding="utf-8"))

        self.assertEqual(location, "inbox.mail")
        self.assertEqual(saved["inbox"]["mail"], "forward.target@163.com")
        self.assertEqual(saved["mail"], "user@icloud.com")
        self.assertEqual(saved["apple"]["cookie"], "TOKEN")

    def test_account_apple_id_is_written_without_losing_apple_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "account.yaml"
            path.write_text(
                "mail: alias@icloud.com\napple:\n  app_password: SECRET\n  cookie: TOKEN\n",
                encoding="utf-8",
            )

            location = update_account_apple_id(
                path,
                "Primary.Login@outlook.com",
                backup=False,
            )
            saved = yaml.safe_load(path.read_text(encoding="utf-8"))

        self.assertEqual(location, "apple.appleid")
        self.assertEqual(saved["apple"]["appleid"], "primary.login@outlook.com")
        self.assertEqual(saved["apple"]["app_password"], "SECRET")
        self.assertEqual(saved["apple"]["cookie"], "TOKEN")

    def test_hme_validation_exposes_primary_apple_id_not_alias(self) -> None:
        settings = Settings(
            app_name="test",
            debug=False,
            domain="icloud.com.cn",
            accounts_files="accounts/",
            client_build="BUILD",
            client_id="CLIENT",
            camoufox_dir="",
            camoufox_proxy="",
            log_dir="",
            base_dir=Path.cwd(),
        )
        response = {
            "webservices": {"maildomainws": {"url": "https://mail.example.test"}},
            "dsInfo": {
                "appleId": "Primary.Login@outlook.com",
                "primaryEmail": "primary.login@outlook.com",
                "appleIdAliases": ["alias@icloud.com"],
            },
        }

        with ICloudHMEClient(settings, NORMALIZED_COOKIE) as client:
            with patch.object(client, "_request", return_value=response):
                client.validate_and_get_api_base(force=True)
                apple_id = client.get_account_apple_id()

        self.assertEqual(apple_id, "primary.login@outlook.com")

    def test_account_inbox_mail_is_overwritten_and_siblings_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "account.yaml"
            path.write_text(
                "inbox:\n  mail: old@example.com\n  provider: 163mail\n",
                encoding="utf-8",
            )

            update_account_inbox(path, "new@example.com", backup=False)
            saved = yaml.safe_load(path.read_text(encoding="utf-8"))

        self.assertEqual(saved["inbox"]["mail"], "new@example.com")
        self.assertEqual(saved["inbox"]["provider"], "163mail")

    def test_apple_id_does_not_replace_icloud_imap_username(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "user@icloud.com.yaml"
            path.write_text(
                "mail: user@icloud.com\n"
                "apple:\n"
                "  appleid: primary@outlook.com\n"
                "  app_password: APPLE_SECRET\n"
                "  cookie: TOKEN\n"
                "inbox:\n"
                "  mail: primary@outlook.com\n",
                encoding="utf-8",
            )
            account = parse_accounts_file(path)[0]

        endpoint = account.resolve_inbox()
        self.assertEqual(account.apple_id, "primary@outlook.com")
        self.assertEqual(account.providers["apple"].mail, "user@icloud.com")
        self.assertEqual(endpoint.name, "apple")
        self.assertEqual(endpoint.mail, "user@icloud.com")
        self.assertTrue(account.mail_ready)

    def test_matching_163_provider_wins_over_apple_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "user@icloud.com.yaml"
            path.write_text(
                "mail: user@icloud.com\n"
                "apple:\n"
                "  appleid: user@163.com\n"
                "  app_password: APPLE_SECRET\n"
                "  cookie: TOKEN\n"
                "163mail:\n"
                "  mail: user@163.com\n"
                "  imap: MAIL_SECRET\n"
                "inbox:\n"
                "  mail: user@163.com\n",
                encoding="utf-8",
            )
            account = parse_accounts_file(path)[0]

        endpoint = account.resolve_inbox()
        self.assertEqual(endpoint.name, "163mail")
        self.assertEqual(endpoint.mail, "user@163.com")
        self.assertEqual(endpoint.password, "MAIL_SECRET")


if __name__ == "__main__":
    unittest.main()
