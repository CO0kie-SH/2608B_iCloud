from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.accounts import parse_accounts_file
from tools.outlook_graph import (
    DEFAULT_CLIENT_ID,
    OutlookGraphClient,
    normalize_graph_message,
    parse_outlook_account_file,
)


CLIENT_ID = "11111111-2222-3333-4444-555555555555"


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self) -> dict:
        return self._payload


class FakeSession:
    def __init__(self, pages: list[dict]) -> None:
        self.pages = list(pages)
        self.post_calls = 0
        self.get_calls = 0

    def post(self, *args, **kwargs) -> FakeResponse:
        self.post_calls += 1
        return FakeResponse({"access_token": "test-access", "expires_in": 3600})

    def get(self, *args, **kwargs) -> FakeResponse:
        self.get_calls += 1
        return FakeResponse(self.pages.pop(0))


def graph_message(message_id: str = "m1") -> dict:
    return {
        "id": message_id,
        "subject": "Your verification code is 384920",
        "from": {"emailAddress": {"name": "Example", "address": "no-reply@example.com"}},
        "sender": {"emailAddress": {"name": "Example", "address": "bounce@example.com"}},
        "toRecipients": [
            {"emailAddress": {"name": "", "address": "alias@icloud.com"}}
        ],
        "receivedDateTime": "2026-08-16T10:01:02Z",
        "sentDateTime": "2026-08-16T10:01:00Z",
        "isRead": False,
        "hasAttachments": False,
        "internetMessageId": "<m1@example.com>",
        "body": {
            "contentType": "html",
            "content": "<p>Verification code: <b>384920</b></p>",
        },
        "bodyPreview": "Verification code: 384920",
        "internetMessageHeaders": [
            {
                "name": "Received-SPF",
                "value": "pass (sender SPF authorized) smtp.mailfrom=bounce@example.com",
            },
            {"name": "Delivered-To", "value": "alias@icloud.com"},
            {"name": "Return-Path", "value": "<bounce@example.com>"},
        ],
    }


class OutlookAccountTests(unittest.TestCase):
    def test_parse_four_columns_and_reversed_token_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            first = Path(temp_dir) / "first.txt"
            first.write_text(
                f"user@outlook.com----password----{CLIENT_ID}----refresh-token",
                encoding="utf-8",
            )
            second = Path(temp_dir) / "second.txt"
            second.write_text(
                f"user@outlook.com----password----refresh-token----{CLIENT_ID}",
                encoding="utf-8",
            )
            self.assertEqual(parse_outlook_account_file(first).client_id, CLIENT_ID)
            self.assertEqual(parse_outlook_account_file(second).client_id, CLIENT_ID)
            self.assertEqual(parse_outlook_account_file(second).refresh_token, "refresh-token")

    def test_missing_client_id_uses_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "account.txt"
            path.write_text(
                "user@outlook.com----password----not-a-uuid----refresh-token",
                encoding="utf-8",
            )
            self.assertEqual(parse_outlook_account_file(path).client_id, DEFAULT_CLIENT_ID)

    def test_yaml_discovers_matching_outlook_token_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            yaml_path = base / "owner@icloud.com.yaml"
            yaml_path.write_text(
                "mail: owner@icloud.com\ninbox:\n  mail: owner@outlook.com\n",
                encoding="utf-8",
            )
            (base / "owner@outlook.com.txt").write_text(
                f"owner@outlook.com----password----{CLIENT_ID}----refresh-token",
                encoding="utf-8",
            )
            account = parse_accounts_file(yaml_path)[0]
            endpoint = account.resolve_inbox()
            self.assertIsNotNone(endpoint)
            self.assertEqual(endpoint.name, "outlook")
            self.assertTrue(account.mail_ready)


class OutlookGraphTests(unittest.TestCase):
    def test_normalize_message_extracts_code_and_received_spf(self) -> None:
        item = normalize_graph_message(
            graph_message(), mailbox="INBOX", account="owner@outlook.com"
        )
        self.assertEqual(item["type"], "code")
        self.assertEqual(item["code"], "384920")
        self.assertIn("smtp.mailfrom=bounce@example.com", item["received_spf"])
        self.assertEqual(item["alias_candidates"][0], "alias@icloud.com")
        self.assertIn("Verification code", item["body_text"])

    def test_delta_pagination_returns_last_cursor(self) -> None:
        next_link = "https://graph.microsoft.com/v1.0/next-page"
        delta_link = "https://graph.microsoft.com/v1.0/delta-page"
        session = FakeSession(
            [
                {"value": [graph_message("m1")], "@odata.nextLink": next_link},
                {"value": [graph_message("m2")], "@odata.deltaLink": delta_link},
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            token_file = Path(temp_dir) / "owner@outlook.com.txt"
            token_file.write_text(
                f"owner@outlook.com----password----{CLIENT_ID}----refresh-token",
                encoding="utf-8",
            )
            client = OutlookGraphClient(token_file, session=session)
            items, cursor = client.fetch_since_cursor(mailbox="INBOX", limit=2)
        self.assertEqual([item["uid"] for item in items], ["m1", "m2"])
        self.assertEqual(cursor, delta_link)
        self.assertEqual(session.post_calls, 1)
        self.assertEqual(session.get_calls, 2)


if __name__ == "__main__":
    unittest.main()
