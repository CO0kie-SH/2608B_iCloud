"""Open a project-local Patchright browser with the DuckDuckGo extension."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlencode


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.patchright_browser import (  # noqa: E402
    BROWSERS_DIR,
    resolve_browser_proxy,
    verify_extension,
)
from tools.config import config_value  # noqa: E402
from tools.duckduckgo_extension import (  # noqa: E402
    ExtensionError,
    ExtensionLock,
    browser_version,
    validate_manifest,
)


EXTENSION_DIR = PROJECT_ROOT / "browsers" / "extensions" / "duckduckgo" / "active"
PROFILE_DIR = PROJECT_ROOT / "db" / "browser_profiles" / "duckduckgo"
NO_EXTENSION_PROFILE_DIR = PROJECT_ROOT / "db" / "browser_profiles" / "duckduckgo-no-extension"
DEFAULT_URL = "https://duckduckgo.com/"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", default=DEFAULT_URL, help="Page to open")
    parser.add_argument("--profile-dir", type=Path, help="Persistent profile; defaults to a separate project-local profile for each mode")
    parser.add_argument(
        "--proxy",
        default=None,
        help="HTTP/HTTPS/SOCKS5 proxy; defaults to PATCHRIGHT_PROXY from .env",
    )
    parser.add_argument("--no-extension", action="store_true", help="Open a comparison session without DuckDuckGo")
    extension_page = parser.add_mutually_exclusive_group()
    extension_page.add_argument("--popup", action="store_true", help="Open the DuckDuckGo popup in a second tab")
    extension_page.add_argument("--options", action="store_true", help="Open the DuckDuckGo settings page in a second tab")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--check", action="store_true", help="Close after loading the requested page")
    parser.add_argument("--screenshot", type=Path, help="Save a full-page screenshot of the requested page")
    return parser


def extension_page_url(extension_id: str, page: str, tab_id: int | None = None) -> str:
    """Build an internal URL while keeping page selection explicit."""
    if page not in {"dashboard/html/browser.html", "html/options.html"}:
        raise ExtensionError(f"Unsupported DuckDuckGo extension page: {page}")
    url = f"chrome-extension://{extension_id}/{page}"
    if tab_id is not None:
        url += "?" + urlencode({"tabId": tab_id})
    return url


def open_extension_page(context, extension: dict, page, *, options: bool = False):
    target = "html/options.html" if options else "dashboard/html/browser.html"
    tab_id = None
    if not options:
        # The dashboard otherwise inspects its own tab instead of the website.
        page.bring_to_front()
        worker = next((worker for worker in context.service_workers if worker.url == extension["service_worker"]), None)
        if worker is None:
            raise ExtensionError("DuckDuckGo service worker is no longer running. Restart the browser.")
        tab_id = worker.evaluate(
            """async (url) => {
                const tabs = await chrome.tabs.query({active: true});
                return tabs.find(tab => tab.url === url)?.id;
            }""",
            page.url,
            isolated_context=False,
        )
        if not isinstance(tab_id, int):
            raise ExtensionError("DuckDuckGo dashboard could not identify the target browser tab.")
    extra_page = context.new_page()
    extra_page.goto(extension_page_url(extension["id"], target, tab_id), wait_until="domcontentloaded")
    return extra_page


def open_browser(args: argparse.Namespace) -> int:
    if args.no_extension and (args.popup or args.options):
        raise ExtensionError("--popup/--options requires the DuckDuckGo extension.")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_DIR)
    from patchright.sync_api import sync_playwright

    raw_proxy = args.proxy if args.proxy is not None else config_value("PATCHRIGHT_PROXY", "")
    browser_proxy = resolve_browser_proxy(raw_proxy)
    launch_args = [] if browser_proxy else ["--no-proxy-server"]
    profile_dir = (args.profile_dir or (NO_EXTENSION_PROFILE_DIR if args.no_extension else PROFILE_DIR)).resolve()

    with ExitStack() as stack:
        extension_dir = None if args.no_extension else EXTENSION_DIR.resolve()
        manifest = None
        if extension_dir is not None:
            if "," in str(extension_dir):
                raise ExtensionError("Extension directory must not contain commas.")
            stack.enter_context(ExtensionLock(extension_dir))
            manifest = validate_manifest(extension_dir, browser_version())
            launch_args.extend([
                f"--disable-extensions-except={extension_dir}",
                f"--load-extension={extension_dir}",
            ])
        else:
            launch_args.append("--disable-extensions")

        playwright = stack.enter_context(sync_playwright())
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chromium",
            headless=args.headless,
            no_viewport=True,
            proxy=browser_proxy,
            args=launch_args,
        )
        try:
            extension = None
            if manifest is not None:
                validate_manifest(extension_dir, context.browser.version)
                extension = verify_extension(context, manifest)

            page = context.pages[0] if context.pages else context.new_page()
            response = page.goto(args.url, wait_until="domcontentloaded", timeout=90_000)
            page.locator("body").wait_for(state="attached", timeout=30_000)
            if args.screenshot:
                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(args.screenshot), full_page=True, timeout=30_000)

            extra_page = None
            if args.popup or args.options:
                extra_page = open_extension_page(context, extension, page, options=args.options)

            result = {
                "url": page.url,
                "title": page.title(),
                "http_status": response.status if response else None,
                "browser_version": context.browser.version,
                "executable": playwright.chromium.executable_path,
                "profile_dir": str(profile_dir),
                "proxy": "configured" if browser_proxy else "direct",
                "extension": extension,
                "extension_popup": extra_page.url if args.popup and extra_page else None,
                "extension_options": extra_page.url if args.options and extra_page else None,
                "screenshot": str(args.screenshot.resolve()) if args.screenshot else None,
            }
            print(json.dumps(result, ensure_ascii=True), flush=True)
            if args.check:
                return 0 if response is None or response.ok else 1
            context.wait_for_event("close", timeout=0)
            return 0
        finally:
            context.close()


def main(argv: list[str] | None = None) -> int:
    from patchright.sync_api import Error as BrowserError

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.no_extension and (args.popup or args.options):
        parser.error("--popup/--options requires the DuckDuckGo extension")
    if args.headless and not args.check:
        parser.error("--headless requires --check so the session exits after loading")
    try:
        return open_browser(args)
    except (ExtensionError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except BrowserError as exc:
        # Launch errors may include proxy credentials in Chromium's command line.
        code = re.search(r"net::[A-Z0-9_]+", str(exc))
        reason = code.group(0) if code else type(exc).__name__
        print(f"ERROR: {reason}. Check the target URL, PATCHRIGHT_PROXY, browser installation, and profile usage.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
