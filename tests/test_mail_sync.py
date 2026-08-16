from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.db import AliasDB
from tools.mail_sync import MailSyncService


class MailSyncTests(unittest.TestCase):
    def test_outlook_cursor_sync_uses_common_mail_table(self) -> None:
        class FakeGraphClient:
            supports_sync_cursor = True

            def fetch_since_cursor(self, **kwargs):
                return (
                    [
                        {
                            "uid": "graph-message-id",
                            "flags": [],
                            "subject": "Code 654321",
                            "type": "code",
                            "code": "654321",
                            "summary": "Verification code 654321",
                            "received_spf": "pass smtp.mailfrom=test@example.com",
                            "alias_candidates": [],
                        }
                    ],
                    "https://graph.microsoft.com/v1.0/delta-cursor",
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            db = AliasDB(Path(temp_dir) / "aliases.db")
            service = MailSyncService(db)
            account = SimpleNamespace(
                name="owner@icloud.com",
                mail="owner@icloud.com",
                inbox_mail="owner@outlook.com",
                mail_ready=True,
            )
            with patch("tools.mail_sync.mail_client_from_account", return_value=FakeGraphClient()):
                stats = service.sync_account(account, mailboxes=["INBOX"])

            record = db.get_mail("owner@icloud.com", "INBOX", "graph-message-id")
            state = db.get_sync_state("owner@icloud.com", "INBOX")
            self.assertEqual(stats[0].saved, 1)
            self.assertEqual(record.code, "654321")
            self.assertIn("smtp.mailfrom", record.received_spf)
            self.assertTrue(state["sync_cursor"].endswith("delta-cursor"))

    def test_all_account_sync_records_unready_account_as_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = MailSyncService(AliasDB(Path(temp_dir) / "aliases.db"))
            account = SimpleNamespace(
                name="missing@icloud.com",
                inbox_mail="missing@163.com",
                mail_ready=False,
            )
            progress: list[str] = []
            stats = service.sync_accounts(
                [account],
                skip_unready=True,
                on_progress=progress.append,
            )

        self.assertEqual(len(stats), 1)
        self.assertTrue(stats[0].ok)
        self.assertTrue(stats[0].skipped)
        self.assertEqual(stats[0].error, "")
        self.assertIn("missing@163.com", stats[0].note)
        self.assertTrue(any("收件 跳过" in line for line in progress))

    def test_single_account_sync_keeps_actionable_missing_provider_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = MailSyncService(AliasDB(Path(temp_dir) / "aliases.db"))
            account = SimpleNamespace(
                name="missing@icloud.com",
                inbox_mail="missing@163.com",
                mail_ready=False,
            )
            stats = service.sync_accounts([account])

        self.assertFalse(stats[0].ok)
        self.assertFalse(stats[0].skipped)
        self.assertIn("inbox=missing@163.com", stats[0].error)


if __name__ == "__main__":
    unittest.main()
