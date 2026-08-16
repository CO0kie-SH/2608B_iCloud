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


if __name__ == "__main__":
    unittest.main()
