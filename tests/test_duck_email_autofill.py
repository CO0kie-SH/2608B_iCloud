from __future__ import annotations

import io
import json
import unittest
from unittest.mock import MagicMock, Mock, patch

from duck.email_autofill import (
    AUTOFILL_URL,
    BLOCKED_MESSAGE,
    EgressBlockedError,
    check_country,
    main,
    parse_mayips_payload,
)


class DuckEmailAutofillTests(unittest.TestCase):
    def test_parses_mayips_payload_and_accepts_non_cn_country(self):
        payload = parse_mayips_payload('{"country":"US","ip":"203.0.113.7"}')
        self.assertEqual(check_country(payload), "US")

    def test_cn_is_blocked_with_exact_message(self):
        with self.assertRaisesRegex(EgressBlockedError, f"^{BLOCKED_MESSAGE}$"):
            check_country({"country": "cn", "ip": "112.64.64.50"})

    def test_missing_or_invalid_country_is_blocked_before_navigation(self):
        for value in ("", "[]", "not-json", '{"ip":"203.0.113.7"}'):
            with self.subTest(value=value), self.assertRaises(EgressBlockedError):
                payload = parse_mayips_payload(value)
                check_country(payload)

    def test_main_reports_cn_without_running_browser(self):
        with patch("duck.email_autofill.run", side_effect=EgressBlockedError(BLOCKED_MESSAGE)):
            with patch("sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertEqual(main([]), 1)
        self.assertEqual(stderr.getvalue().strip(), f"ERROR: {BLOCKED_MESSAGE}")

    def test_autofill_url_is_the_second_step_target(self):
        self.assertEqual(AUTOFILL_URL, "https://duckduckgo.com/email/settings/autofill")

    def test_cn_gate_does_not_create_or_navigate_second_page(self):
        page = Mock()
        page.goto.return_value = Mock(status=200)
        page.locator.return_value.wait_for.return_value = None
        page.locator.return_value.inner_text.return_value = json.dumps({"country": "CN", "ip": "112.64.64.50"})
        context = Mock(pages=[page])
        context.browser.version = "151.0.7922.34"
        playwright = Mock()
        playwright.chromium.launch_persistent_context.return_value = context
        playwright_manager = MagicMock()
        playwright_manager.__enter__.return_value = playwright
        playwright_manager.__exit__.return_value = False
        lock = MagicMock()
        lock.__enter__.return_value = lock
        lock.__exit__.return_value = False
        with patch("patchright.sync_api.sync_playwright", return_value=playwright_manager), \
                patch("tools.duckduckgo_extension.ExtensionLock", return_value=lock), \
                patch("tools.duckduckgo_extension.validate_manifest", return_value={"version": "2026.8.24"}), \
                patch("tools.duckduckgo_extension.browser_version", return_value="151.0.7922.34"), \
                patch("scripts.patchright_browser.verify_extension", return_value={"id": "fixture"}), \
                patch("scripts.patchright_browser.resolve_browser_proxy", return_value=None), \
                patch("tools.config.config_value", return_value="direct"):
            with self.assertRaisesRegex(EgressBlockedError, f"^{BLOCKED_MESSAGE}$"):
                from duck.email_autofill import run, build_parser
                run(build_parser().parse_args(["--headless", "--proxy", "direct"]))
        page.goto.assert_called_once_with("https://mayips.com/", wait_until="domcontentloaded", timeout=90000)
        context.new_page.assert_not_called()


if __name__ == "__main__":
    unittest.main()
