from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

import main
from tools.accounts import create_account_file, parse_accounts_file
from tools.config import Settings


class AccountBootstrapTests(unittest.TestCase):
    def test_cookie_login_parser_supports_headless_forward_capture(self) -> None:
        args = main.build_parser().parse_args(
            ["cookie-login", "-a", "user@icloud.com", "--headless"]
        )

        self.assertTrue(args.headless)
        self.assertFalse(args.no_forward_to)

    def test_create_account_file_builds_minimal_cn_account(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            path, created = create_account_file(
                "accounts/",
                base_dir,
                "New.User@iCloud.com",
                domain="icloud.com.cn",
            )
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            account = parse_accounts_file(path)[0]

        self.assertTrue(created)
        self.assertEqual(path.name, "new.user@icloud.com.yaml")
        self.assertEqual(data["mail"], "new.user@icloud.com")
        self.assertEqual(data["apple"]["domain"], "icloud.com.cn")
        self.assertEqual(data["apple"]["appleid"], "new.user@icloud.com")
        self.assertEqual(account.icloud_domain, "icloud.com.cn")
        self.assertEqual(account.format_errors, [])

    def test_existing_account_file_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            path, _ = create_account_file(
                "accounts/", base_dir, "user@icloud.com", domain="icloud.com"
            )
            original = path.read_text(encoding="utf-8")
            same_path, created = create_account_file(
                "accounts/", base_dir, "user@icloud.com", domain="icloud.com.cn"
            )
            current = path.read_text(encoding="utf-8")

        self.assertFalse(created)
        self.assertEqual(same_path, path)
        self.assertEqual(original, current)

    def test_cookie_login_picker_creates_missing_account(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
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
                base_dir=Path(temp_dir),
            )
            args = argparse.Namespace(region="cn", suffix=None, domain=None)
            with patch("main.load_settings", return_value=settings):
                selected_settings, account, created_path = (
                    main._pick_or_create_cookie_account("user005@icloud.com", args)
                )

        self.assertIsNotNone(created_path)
        self.assertEqual(selected_settings.domain, "icloud.com.cn")
        self.assertEqual(account.name, "user005@icloud.com")
        self.assertEqual(account.icloud_domain, "icloud.com.cn")

    def test_unsafe_or_malformed_account_names_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            for name in ("../account", "a@b@icloud.com", "a..b@icloud.com", "a@icloud"):
                with self.subTest(name=name):
                    with self.assertRaises(ValueError):
                        create_account_file(
                            "accounts/", base_dir, name, domain="icloud.com"
                        )


if __name__ == "__main__":
    unittest.main()
