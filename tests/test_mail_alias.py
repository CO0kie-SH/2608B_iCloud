from __future__ import annotations

import unittest
from types import SimpleNamespace

from tools.mail_alias import MailAliasExtractor


def account(provider: str = "163mail") -> SimpleNamespace:
    inbox_mail = {
        "163mail": "owner@163.com",
        "outlook": "owner@outlook.com",
        "apple": "owner@icloud.com",
    }.get(provider, "owner@example.com")
    endpoint = SimpleNamespace(name=provider, mail=inbox_mail)
    return SimpleNamespace(
        name="owner@icloud.com",
        mail="owner@icloud.com",
        resolve_inbox=lambda: endpoint,
    )


class MailAliasExtractorTests(unittest.TestCase):
    def test_163_verp_envelope_has_priority_and_preserves_plus_tag(self) -> None:
        extractor = MailAliasExtractor([account()])
        match = extractor.extract(
            {
                "account": "owner@icloud.com",
                "envelope_from": (
                    "bounces+20216706-6f1d-byline28_hugest+2=icloud.com"
                    "@em7877.tm.openai.com"
                ),
                "alias_hme": "byline28_hugest@icloud.com",
                "to_addr": "Hide My Email <byline28_hugest@icloud.com>",
            }
        )
        self.assertEqual(match.address, "byline28_hugest+2@icloud.com")
        self.assertEqual(match.source, "envelope_from_verp")

    def test_163_verp_envelope_supports_alias_without_plus_tag(self) -> None:
        decoded = MailAliasExtractor.decode_verp_alias(
            "bounces+20216706-a5e1-lessees_homiest_7t=icloud.com"
            "@em7877.tm.openai.com"
        )
        self.assertEqual(decoded, "lessees_homiest_7t@icloud.com")

    def test_apple_return_path_supports_single_bounce_prefix_and_hyphen(self) -> None:
        extractor = MailAliasExtractor([account("apple")])
        match = extractor.extract(
            {
                "account": "owner@icloud.com",
                "return_path": "bounce+57ca0c.b462b7-sheaves.sud-0c+1=icloud.com@tm1.openai.com",
                "alias_hme": "sheaves.sud-0c@icloud.com",
            }
        )
        self.assertEqual(match.address, "sheaves.sud-0c+1@icloud.com")
        self.assertEqual(match.source, "return_path_verp")

    def test_outlook_return_path_keeps_plus_tag(self) -> None:
        extractor = MailAliasExtractor([account("outlook")])
        match = extractor.extract(
            {
                "account": "owner@icloud.com",
                "return_path": "bounces+20216706-7d2b-owner+5=outlook.com@em7877.tm.openai.com",
            }
        )
        self.assertEqual(match.address, "owner+5@outlook.com")

    def test_outlook_base_inbox_verp_is_not_an_alias(self) -> None:
        extractor = MailAliasExtractor([account("outlook")])
        match = extractor.extract(
            {
                "account": "owner@icloud.com",
                "return_path": "bounces+20216706-2f17-owner=outlook.com@em7877.tm.openai.com",
            }
        )
        self.assertFalse(match.matched)

    def test_explicit_hme_alias_has_priority(self) -> None:
        extractor = MailAliasExtractor([account()])
        match = extractor.extract(
            {
                "account": "owner@icloud.com",
                "alias_hme": "known_alias@icloud.com",
                "to_addr": "other_alias@icloud.com",
            }
        )
        self.assertEqual(match.address, "known_alias@icloud.com")
        self.assertEqual(match.source, "alias_hme")

    def test_163_falls_back_to_graph_style_to_address(self) -> None:
        extractor = MailAliasExtractor([account()])
        match = extractor.extract(
            {
                "account": "owner@icloud.com",
                "parent_mail": "owner@icloud.com",
                "to_addr": "new_alias@icloud.com <new_alias@icloud.com>",
            }
        )
        self.assertEqual(match.address, "new_alias@icloud.com")
        self.assertEqual(match.source, "to_addr")
        self.assertEqual(match.provider, "163mail")

    def test_163_direct_inbox_is_not_an_alias(self) -> None:
        extractor = MailAliasExtractor([account()])
        match = extractor.extract(
            {
                "account": "owner@icloud.com",
                "parent_mail": "owner@icloud.com",
                "to_addr": "owner@163.com",
            }
        )
        self.assertFalse(match.matched)

    def test_provider_specific_fallback_is_not_applied_to_apple(self) -> None:
        extractor = MailAliasExtractor([account("apple")])
        match = extractor.extract(
            {
                "account": "owner@icloud.com",
                "to_addr": "other_alias@icloud.com",
            }
        )
        self.assertFalse(match.matched)


if __name__ == "__main__":
    unittest.main()
