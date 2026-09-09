from __future__ import annotations

import itertools
import json
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
import unittest

from mail_alias.simple import addresses, decode_verp, extract_alias
from tools.mail_alias import MailAliasExtractor


ROOT = Path(__file__).resolve().parents[1]
OWNER = "owner@icloud.com"
VERP = "bounces+12345-a1b2-demo+2=icloud.com@relay.example.com"
RETURN_PATH = "bounce+campaign-demo-name+1=icloud.com@relay.example.com"


class SimpleMailAliasTests(unittest.TestCase):
    def extract(self, provider="163mail", **fields):
        return extract_alias({"account": OWNER, **fields}, provider=provider, inbox_mail="owner@163.com")

    def test_163_priority_and_each_fallback(self):
        fields = {
            "envelope_from": VERP,
            "return_path": RETURN_PATH,
            "alias_hme": "explicit@icloud.com",
            "delivered_to": "delivered@icloud.com",
            "to_addr": "to@icloud.com",
        }
        for field, source, address in (
            ("envelope_from", "envelope_from_verp", "demo+2@icloud.com"),
            ("return_path", "return_path_verp", "demo-name+1@icloud.com"),
            ("alias_hme", "alias_hme", "explicit@icloud.com"),
            ("delivered_to", "delivered_to", "delivered@icloud.com"),
            ("to_addr", "to_addr", "to@icloud.com"),
        ):
            with self.subTest(source=source):
                self.assertEqual(self.extract(**fields), {
                    "address": address, "source": source, "provider": "163mail", "matched": True,
                })
                fields.pop(field)
        self.assertEqual(self.extract(), {"address": "", "source": "", "provider": "163mail", "matched": False})

    def test_apple_and_outlook_only_use_return_path_then_explicit(self):
        for provider in ("apple", "outlook"):
            with self.subTest(provider=provider):
                fields = {"envelope_from": VERP, "return_path": RETURN_PATH, "alias_hme": "known@icloud.com"}
                self.assertEqual(self.extract(provider, **fields)["source"], "return_path_verp")
                fields["return_path"] = "invalid"
                self.assertEqual(self.extract(provider, **fields)["address"], "known@icloud.com")
                fields.pop("alias_hme")
                fields.update(delivered_to="delivered@icloud.com", to_addr="to@icloud.com")
                self.assertFalse(self.extract(provider, **fields)["matched"])

    def test_unknown_provider_only_uses_explicit_alias(self):
        for provider in ("qq", "", "icloud"):
            with self.subTest(provider=provider):
                self.assertFalse(self.extract(provider, envelope_from=VERP, return_path=RETURN_PATH)["matched"])
                self.assertEqual(self.extract(provider, alias_hme="known@icloud.com")["address"], "known@icloud.com")
        self.assertEqual(self.extract(" APPLE ", return_path=RETURN_PATH)["provider"], "apple")

    def test_excludes_record_and_context_addresses_but_keeps_plus_tag(self):
        context = {"account": OWNER, "parent_mail": "parent@icloud.com", "inbox_mail": "owner@outlook.com"}
        record = {"account": "record@icloud.com", "parent_mail": "record-parent@icloud.com"}
        for address in (*context.values(), *record.values()):
            with self.subTest(address=address):
                envelope = f"bounces+12345-a1b2-{address.replace('@', '=')}@relay.example.com"
                result = extract_alias({**record, "return_path": envelope}, provider="outlook", **context)
                self.assertFalse(result["matched"])
        envelope = "bounces+12345-a1b2-owner+5=outlook.com@relay.example.com"
        result = extract_alias({**record, "return_path": envelope}, provider="outlook", **context)
        self.assertEqual(result["address"], "owner+5@outlook.com")
        self.assertFalse(self.extract(to_addr="owner@163.com")["matched"])
        self.assertEqual(self.extract(to_addr="owner@163.com, Demo <demo@icloud.com>")["address"], "demo@icloud.com")

    def test_explicit_base_address_remains_trusted_like_production(self):
        self.assertEqual(self.extract(alias_hme=OWNER)["address"], OWNER)

    def test_verp_formats_and_invalid_inputs_match_production(self):
        fixtures = (
            (VERP, "demo+2@icloud.com"),
            (RETURN_PATH, "demo-name+1@icloud.com"),
            (VERP.replace("demo+2", "demo_name"), "demo_name@icloud.com"),
            (VERP.upper(), "demo+2@icloud.com"),
            ("bounce+campaign-demo=part=icloud.com@relay.example.com", "demo=part@icloud.com"),
            ("bounce+campaign-abcd-demo=icloud.com@relay.example.com", "demo@icloud.com"),
            ("bounce+campaign-abc-demo=icloud.com@relay.example.com", "abc-demo@icloud.com"),
            ("bounce+campaign-zzzz-demo=icloud.com@relay.example.com", "zzzz-demo@icloud.com"),
            ("first@example.com, " + VERP, ""),
        )
        invalid = (
            None, "", "invalid", "plain@example.com", "bounce+-demo=icloud.com@relay.example.com",
            "bounce+campaign-@relay.example.com", "bounce+campaign-demo@relay.example.com",
            "bounce+campaign-=icloud.com@relay.example.com", "bounce+campaign-demo=@relay.example.com",
            "bounce+campaign-demo=bad_domain@relay.example.com",
        )
        for value, expected in (*fixtures, *((value, "") for value in invalid)):
            with self.subTest(value=value):
                self.assertEqual(decode_verp(value), expected)
                self.assertEqual(decode_verp(value), MailAliasExtractor.decode_verp_alias(value))

    def test_address_parser_supports_graph_format_and_deduplication(self):
        for value, expected in (
            ("DEMO+2@icloud.com <DEMO+2@icloud.com>", ["demo+2@icloud.com"]),
            ("Demo <demo@icloud.com>, demo@icloud.com", ["demo@icloud.com"]),
            (None, []),
        ):
            with self.subTest(value=value):
                self.assertEqual(addresses(value), expected)
                self.assertEqual(addresses(value), MailAliasExtractor.addresses(value))

    def test_priority_combinations_match_production(self):
        for provider in ("163mail", "apple", "outlook", "qq"):
            endpoint = SimpleNamespace(name=provider, mail="owner@163.com")
            account = SimpleNamespace(name=OWNER, mail=OWNER, resolve_inbox=lambda: endpoint)
            production = MailAliasExtractor([account])
            for envelope, return_path, explicit, delivered in itertools.product(
                (None, "invalid", VERP), (None, "invalid", RETURN_PATH),
                (None, OWNER, "known@icloud.com"), (None, "owner@163.com", "delivered@icloud.com"),
            ):
                fields = {
                    "account": OWNER, "envelope_from": envelope, "return_path": return_path,
                    "alias_hme": explicit, "delivered_to": delivered, "to_addr": "to@icloud.com",
                }
                with self.subTest(provider=provider, fields=fields):
                    self.assertEqual(self.extract(provider, **fields), production.extract(fields).to_dict())

    def test_demo_runs_in_isolated_python_without_project_imports(self):
        completed = subprocess.run(
            [sys.executable, "-I", str(ROOT / "mail_alias/simple.py")],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(json.loads(completed.stdout), {
            "address": "demo+2@icloud.com", "source": "envelope_from_verp", "provider": "163mail", "matched": True,
        })

    def test_readme_python_example_runs(self):
        readme = (ROOT / "mail_alias/README.md").read_text(encoding="utf-8")
        snippets = re.findall(r"```python\n(.*?)```", readme, re.S)
        self.assertEqual(len(snippets), 1)
        exec(compile(snippets[0], "mail_alias/README.md", "exec"), {})


if __name__ == "__main__":
    unittest.main()
