from __future__ import annotations

import io
import json
import os
import unittest
from unittest.mock import MagicMock, Mock, patch

from patchright.sync_api import Error as BrowserError

from duck import duck_browser


class DuckBrowserTests(unittest.TestCase):
    def test_defaults_use_duckduckgo_and_dedicated_profile(self):
        args = duck_browser.build_parser().parse_args([])
        self.assertEqual(args.url, duck_browser.DEFAULT_URL)
        self.assertIsNone(args.profile_dir)
        self.assertTrue(duck_browser.PROFILE_DIR.is_relative_to(duck_browser.PROJECT_ROOT / "db"))
        self.assertFalse(args.no_extension)
        self.assertFalse(args.popup)
        self.assertFalse(args.options)

    def test_extension_page_urls_are_restricted_to_known_pages(self):
        self.assertEqual(
            duck_browser.extension_page_url("EXTENSION_ID", "dashboard/html/browser.html"),
            "chrome-extension://EXTENSION_ID/dashboard/html/browser.html",
        )
        self.assertEqual(
            duck_browser.extension_page_url("EXTENSION_ID", "html/options.html"),
            "chrome-extension://EXTENSION_ID/html/options.html",
        )
        with self.assertRaises(duck_browser.ExtensionError):
            duck_browser.extension_page_url("EXTENSION_ID", "public/js/background.js")

    def test_invalid_mode_combinations_fail_before_launch(self):
        for args in (["--popup", "--options"], ["--popup", "--no-extension"], ["--headless"]):
            with self.subTest(args=args), patch.object(duck_browser, "open_browser") as launch:
                with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as error:
                    duck_browser.main(args)
                self.assertEqual(error.exception.code, 2)
                launch.assert_not_called()


class DuckBrowserLaunchTests(unittest.TestCase):
    def setUp(self):
        self.page = Mock(url="https://example.org/")
        self.page.title.return_value = "Example Domain"
        self.response = Mock(status=200, ok=True)
        self.page.goto.return_value = self.response
        self.context = Mock(pages=[self.page])
        self.context.browser.version = "151.0.7922.34"
        self.extension = {
            "id": "fixture-extension",
            "version": "2026.8.24",
            "service_worker": "chrome-extension://fixture-extension/public/js/background.js",
        }
        self.worker = Mock(url=self.extension["service_worker"])
        self.worker.evaluate.return_value = 42
        self.context.service_workers = [self.worker]
        self.extra_page = Mock(url="chrome-extension://fixture-extension/html/options.html")
        self.context.new_page.return_value = self.extra_page
        self.playwright = Mock()
        self.playwright.chromium.executable_path = "fixture-chromium"
        self.playwright.chromium.launch_persistent_context.return_value = self.context
        manager = MagicMock()
        manager.__enter__.return_value = self.playwright
        patches = {
            "runtime": patch("patchright.sync_api.sync_playwright", return_value=manager),
            "lock": patch.object(duck_browser, "ExtensionLock"),
            "manifest": patch.object(duck_browser, "validate_manifest", return_value={"version": "2026.8.24"}),
            "version": patch.object(duck_browser, "browser_version", return_value="151.0.7922.34"),
            "verify": patch.object(duck_browser, "verify_extension", return_value=self.extension),
            "config": patch.object(duck_browser, "config_value", return_value="socks5://127.0.0.1:10808"),
            "stdout": patch("sys.stdout", new_callable=io.StringIO),
            "stderr": patch("sys.stderr", new_callable=io.StringIO),
            "environ": patch.dict(os.environ),
        }
        self.mocks = {}
        for name, patcher in patches.items():
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def test_launch_loads_local_extension_proxy_and_persistent_profile(self):
        self.assertEqual(duck_browser.main(["https://example.org/", "--check"]), 0)
        options = self.playwright.chromium.launch_persistent_context.call_args.kwargs
        self.assertEqual(options["channel"], "chromium")
        self.assertFalse(options["headless"])
        self.assertEqual(options["proxy"], {"server": "socks5://127.0.0.1:10808"})
        self.assertEqual(options["user_data_dir"], str(duck_browser.PROFILE_DIR))
        self.assertIn(f"--load-extension={duck_browser.EXTENSION_DIR}", options["args"])
        self.assertEqual(os.environ["PLAYWRIGHT_BROWSERS_PATH"], str(duck_browser.BROWSERS_DIR))
        self.mocks["verify"].assert_called_once()
        self.context.close.assert_called_once()
        self.context.wait_for_event.assert_not_called()
        output = json.loads(self.mocks["stdout"].getvalue())
        self.assertEqual(output["http_status"], 200)
        self.assertEqual(output["extension"], self.extension)
        self.assertNotIn("127.0.0.1", self.mocks["stdout"].getvalue())

    def test_no_extension_disables_persisted_extensions_and_uses_separate_profile(self):
        self.assertEqual(duck_browser.main(["--no-extension", "--headless", "--check", "--proxy", "direct"]), 0)
        options = self.playwright.chromium.launch_persistent_context.call_args.kwargs
        self.assertTrue(options["headless"])
        self.assertIsNone(options["proxy"])
        self.assertEqual(options["args"], ["--no-proxy-server", "--disable-extensions"])
        self.assertEqual(options["user_data_dir"], str(duck_browser.NO_EXTENSION_PROFILE_DIR))
        self.mocks["lock"].assert_not_called()
        self.mocks["verify"].assert_not_called()
        self.mocks["config"].assert_not_called()

    def test_profile_override_and_manual_window_lifetime(self):
        self.assertEqual(duck_browser.main(["--profile-dir", "db/fixture-profile"]), 0)
        options = self.playwright.chromium.launch_persistent_context.call_args.kwargs
        self.assertEqual(options["user_data_dir"], str(duck_browser.PROJECT_ROOT / "db/fixture-profile"))
        self.context.wait_for_event.assert_called_once_with("close", timeout=0)
        self.context.close.assert_called_once()

    def test_popup_binds_dashboard_to_original_web_tab(self):
        self.assertEqual(duck_browser.main(["--popup", "--check"]), 0)
        self.page.bring_to_front.assert_called_once()
        self.assertEqual(self.worker.evaluate.call_args.args[1], self.page.url)
        self.assertFalse(self.worker.evaluate.call_args.kwargs["isolated_context"])
        self.extra_page.goto.assert_called_once_with(
            "chrome-extension://fixture-extension/dashboard/html/browser.html?tabId=42",
            wait_until="domcontentloaded",
        )

    def test_options_opens_settings_without_querying_web_tab(self):
        self.assertEqual(duck_browser.main(["--options", "--check"]), 0)
        self.worker.evaluate.assert_not_called()
        self.extra_page.goto.assert_called_once_with(
            "chrome-extension://fixture-extension/html/options.html", wait_until="domcontentloaded",
        )

    def test_http_failure_propagates_exit_status_and_closes_context(self):
        self.response.status = 403
        self.response.ok = False
        self.assertEqual(duck_browser.main(["--check"]), 1)
        self.assertEqual(json.loads(self.mocks["stdout"].getvalue())["http_status"], 403)
        self.context.close.assert_called_once()

    def test_navigation_failure_closes_browser_and_redacts_sensitive_error_text(self):
        self.page.goto.side_effect = BrowserError("net::ERR_PROXY_CONNECTION_FAILED http://user:secret@proxy:8080")
        self.assertEqual(duck_browser.main(["--check"]), 1)
        self.context.close.assert_called_once()
        self.assertIn("net::ERR_PROXY_CONNECTION_FAILED", self.mocks["stderr"].getvalue())
        self.assertNotIn("secret", self.mocks["stderr"].getvalue())

    def test_busy_extension_does_not_launch_browser(self):
        self.mocks["lock"].return_value.__enter__.side_effect = duck_browser.ExtensionError("Extension is in use.")
        self.assertEqual(duck_browser.main(["--check"]), 1)
        self.playwright.chromium.launch_persistent_context.assert_not_called()

    def test_extension_verification_failure_closes_browser(self):
        self.mocks["verify"].side_effect = duck_browser.ExtensionError("Extension version mismatch.")
        self.assertEqual(duck_browser.main(["--check"]), 1)
        self.page.goto.assert_not_called()
        self.context.close.assert_called_once()

    def test_check_allows_about_blank_without_http_response(self):
        self.page.goto.return_value = None
        self.assertEqual(duck_browser.main(["about:blank", "--check"]), 0)

    def test_keyboard_interrupt_closes_context(self):
        self.context.wait_for_event.side_effect = KeyboardInterrupt
        self.assertEqual(duck_browser.main([]), 130)
        self.context.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
