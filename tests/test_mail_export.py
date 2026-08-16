from __future__ import annotations

import csv
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tools.db import AliasDB
from tools.mail_alias import MailAliasExtractor
from tools.mail_export import export_verification_codes_csv, set_file_readonly


class MailExportTests(unittest.TestCase):
    def test_export_unlocks_then_restores_readonly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            db = AliasDB(base / "aliases.db")
            db.upsert_mail(
                account="owner@icloud.com",
                uid="message-1",
                mail_type="code",
                code="123456",
                subject="Verification code 123456",
                received_spf="pass smtp.mailfrom=sender@example.com",
                alias_hme="alias@icloud.com",
            )
            output = base / "sava" / "verification_codes.csv"
            output.parent.mkdir(parents=True)
            output.write_text("old", encoding="utf-8")
            set_file_readonly(output)

            endpoint = SimpleNamespace(name="163mail", mail="owner@163.com")
            account = SimpleNamespace(
                name="owner@icloud.com",
                mail="owner@icloud.com",
                resolve_inbox=lambda: endpoint,
            )
            path, count = export_verification_codes_csv(
                db,
                output,
                alias_extractor=MailAliasExtractor([account]),
            )
            self.assertEqual(count, 1)
            self.assertFalse(bool(path.stat().st_mode & stat.S_IWRITE))
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["code"], "123456")
            self.assertIn("smtp.mailfrom", rows[0]["received_spf"])
            self.assertEqual(rows[0]["recipient_alias"], "alias@icloud.com")
            self.assertEqual(rows[0]["parent_mail"], "owner@163.com")


if __name__ == "__main__":
    unittest.main()
