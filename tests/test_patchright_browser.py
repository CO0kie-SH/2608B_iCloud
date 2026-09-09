from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

from scripts import patchright_browser


class PatchrightBrowserTests(unittest.TestCase):
    def test_open_defaults_are_visible_direct_and_project_local(self):
        args = patchright_browser.build_parser().parse_args(["open", "https://mayips.com/"])
        self.assertEqual(args.url, "https://mayips.com/")
        self.assertFalse(args.headless)
        self.assertFalse(args.check)
        self.assertIsNone(args.proxy)
        self.assertEqual(args.profile_dir, patchright_browser.PROFILE_DIR)
        self.assertTrue(args.profile_dir.is_relative_to(patchright_browser.PROJECT_ROOT / "db"))

    def test_install_uses_current_python_and_project_browser_directory(self):
        with patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "external-cache"}):
            with patch.object(patchright_browser.subprocess, "run", return_value=Mock(returncode=0)) as run:
                self.assertEqual(patchright_browser.main(["install"]), 0)
            self.assertEqual(os.environ["PLAYWRIGHT_BROWSERS_PATH"], "external-cache")
        argv = run.call_args.args[0]
        self.assertEqual(argv, [sys.executable, "-m", "patchright", "install", "chromium", "--no-shell"])
        self.assertEqual(
            run.call_args.kwargs["env"]["PLAYWRIGHT_BROWSERS_PATH"],
            str(patchright_browser.PROJECT_ROOT / "browsers" / "patchright"),
        )

    def test_install_propagates_failure(self):
        with patch.object(patchright_browser.subprocess, "run", return_value=Mock(returncode=1)):
            self.assertEqual(patchright_browser.main(["install"]), 1)

    def test_proxy_uses_env_value_and_normalizes_socks5h(self):
        args = patchright_browser.build_parser().parse_args(["open", "about:blank"])
        fake_context = Mock()
        fake_context.pages = []
        fake_context.browser.version = "151.0.7922.34"
        fake_page = Mock()
        fake_page.url = "about:blank"
        fake_page.title.return_value = ""
        fake_page.goto.return_value = None
        fake_page.locator.return_value.wait_for.return_value = None
        fake_context.new_page.return_value = fake_page
        fake_playwright = Mock()
        fake_playwright.chromium.executable_path = "fixture-chromium"
        fake_playwright.chromium.launch_persistent_context.return_value = fake_context
        fake_cm = MagicMock()
        fake_cm.__enter__.return_value = fake_playwright
        fake_cm.__exit__.return_value = False
        with patch.object(patchright_browser, "config_value", return_value="socks5h://127.0.0.1:10808"):
            with patch("patchright.sync_api.sync_playwright", return_value=fake_cm):
                self.assertEqual(patchright_browser.open_browser(args), 0)
        options = fake_playwright.chromium.launch_persistent_context.call_args.kwargs
        self.assertEqual(options["proxy"], {"server": "socks5://127.0.0.1:10808"})
        self.assertEqual(options["args"], [])

    def test_command_line_proxy_overrides_env_and_direct_aliases_disable_proxy(self):
        args = patchright_browser.build_parser().parse_args(["open", "about:blank", "--proxy", "http://proxy:8080"])
        self.assertEqual(args.proxy, "http://proxy:8080")
        self.assertEqual(patchright_browser.resolve_browser_proxy(args.proxy), {"server": "http://proxy:8080"})
        self.assertEqual(patchright_browser.resolve_browser_proxy("none"), None)
        self.assertEqual(patchright_browser.resolve_browser_proxy("direct"), None)
        with self.assertRaises(patchright_browser.ExtensionError):
            patchright_browser.resolve_browser_proxy("ftp://proxy:21")


if __name__ == "__main__":
    unittest.main()
