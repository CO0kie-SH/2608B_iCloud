from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.mailcom import MailComAccount, MailComHeader, load_mailcom_accounts, normalize_message, parse_account_row


class MailComTests(unittest.TestCase):
    def test_parse_and_discover_mailcom_credentials(self) -> None:
        self.assertEqual(parse_account_row("User@Example.com----secret").email, "user@example.com")
        self.assertEqual(
            parse_account_row("User@Example.com----secret----http://127.0.0.1:7897").proxy,
            "http://127.0.0.1:7897",
        )
        self.assertIsNone(parse_account_row("# comment"))
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            accounts = base / "accounts"
            accounts.mkdir()
            (accounts / "mail.com_20260816.txt").write_text(
                "First@example.com----one\nfirst@example.com----duplicate\nSecond@example.com----two\n",
                encoding="utf-8",
            )
            loaded = load_mailcom_accounts(base)
        self.assertEqual([item.email for item in loaded], ["first@example.com", "second@example.com"])
        self.assertEqual(loaded[0].source.endswith("mail.com_20260816.txt"), True)

    def test_client_uses_account_proxy_before_global_proxy(self) -> None:
        account = MailComAccount("user@example.com", "secret", proxy="http://account-proxy:8080")
        with patch.dict("os.environ", {"MAILCOM_PROXY": "http://global-proxy:8080"}, clear=False):
            from tools.mailcom import MailComClient

            client = MailComClient(account)
        self.assertEqual(client.session.proxies["https"], "http://account-proxy:8080")

    def test_client_accepts_socks5h_proxy(self) -> None:
        account = MailComAccount("user@example.com", "secret", proxy="socks5h://127.0.0.1:1080")
        from tools.mailcom import MailComClient

        client = MailComClient(account)
        self.assertEqual(client.session.proxies["http"], "socks5h://127.0.0.1:1080")
        self.assertEqual(client.session.proxies["https"], "socks5h://127.0.0.1:1080")

    def test_normalize_html_message_extracts_code_and_body(self) -> None:
        account = MailComAccount("user@example.com", "secret")
        header = MailComHeader("message-1", "Your verification code", "Service <no-reply@example.net>", "2026-08-16T10:00:00Z")
        payload = normalize_message(account, header, "<html><body>Your code is <b>483920</b></body></html>")
        self.assertEqual(payload["mail_type"], "code")
        self.assertEqual(payload["code"], "483920")
        self.assertIn("Your code is", payload["body_text"])
        self.assertTrue(payload["body_html"])
        self.assertEqual(payload["from_addr"], "no-reply@example.net")


if __name__ == "__main__":
    unittest.main()
