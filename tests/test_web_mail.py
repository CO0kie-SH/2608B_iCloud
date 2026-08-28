from __future__ import annotations

import unittest

from web.schemas import mail_to_out


class WebMailSchemaTests(unittest.TestCase):
    def test_mail_response_exposes_recipient_alias(self) -> None:
        record = type(
            "Record",
            (),
            {
                "to_dict": lambda self: {
                    "id": 1,
                    "account": "owner@icloud.com",
                    "mailbox": "INBOX",
                    "uid": "42",
                    "alias_hme": "known@icloud.com",
                    "from_name": "Example",
                    "from_addr": "no-reply@example.com",
                    "sender_addr": "no-reply@example.com",
                    "return_path": "bounce@example.com",
                    "received_spf": "pass",
                    "envelope_from": "bounce@example.com",
                    "is_relayed": False,
                    "relay_label": "",
                    "to_addr": "known@icloud.com",
                    "delivered_to": "known@icloud.com",
                    "subject": "Code",
                    "mail_type": "code",
                    "code": "123456",
                    "summary": "",
                    "date_header": "",
                    "date_utc": "",
                    "internaldate": "",
                    "size": 10,
                    "is_seen": True,
                    "has_attachment": False,
                    "attachments": [],
                    "body_text_len": 0,
                    "body_html_len": 0,
                }
            },
        )()

        result = mail_to_out(record, recipient_alias="known+tag@icloud.com")
        self.assertEqual(result.recipient_alias, "known+tag@icloud.com")
        self.assertEqual(result.model_dump()["recipient_alias"], "known+tag@icloud.com")


class MailBodyCacheTests(unittest.TestCase):
    def test_save_and_reload_plain_text_without_listing_it(self) -> None:
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from tools.db import AliasDB

        with TemporaryDirectory() as temp_dir:
            db = AliasDB(Path(temp_dir) / "aliases.db")
            db.upsert_mail(
                account="owner@icloud.com",
                uid="99",
                mailbox="INBOX",
                subject="hi",
                from_addr="a@b.com",
            )
            db.save_mail_body_text("owner@icloud.com", "INBOX", "99", "验证码 123456")
            stored = db.get_mail("owner@icloud.com", "INBOX", "99")
            listed = db.list_mails(account="owner@icloud.com")[0]
            self.assertEqual(stored.body_text, "验证码 123456")
            self.assertEqual(stored.body_text_len, len("验证码 123456"))
            self.assertEqual(listed.body_text, "")


if __name__ == "__main__":
    unittest.main()
