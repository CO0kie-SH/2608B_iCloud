"""Install project-local Chromium or open a standalone Patchright session."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.duckduckgo_extension import ExtensionError, ExtensionLock, browser_version, validate_manifest
from tools.config import config_value

BROWSERS_DIR = PROJECT_ROOT / "browsers" / "patchright"
PROFILE_DIR = PROJECT_ROOT / "db" / "browser_profiles" / "patchright"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("install", help="Download Chromium into browsers/patchright")
    open_parser = commands.add_parser("open", help="Open a browser until its window is closed")
    open_parser.add_argument("url", nargs="?", default="about:blank")
    open_parser.add_argument("--profile-dir", type=Path, default=PROFILE_DIR)
    open_parser.add_argument(
        "--proxy",
        default=None,
        help="HTTP/HTTPS/SOCKS5 proxy; defaults to PATCHRIGHT_PROXY from .env",
    )
    open_parser.add_argument("--headless", action="store_true")
    open_parser.add_argument("--check", action="store_true", help="Close after loading the page")
    open_parser.add_argument("--screenshot", type=Path, help="Save a full-page screenshot")
    open_parser.add_argument("--extension-dir", type=Path, help="Load an unpacked Manifest V3 extension")
    return parser


def install_browser() -> int:
    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(BROWSERS_DIR))
    # The full Chromium channel supports both headed and modern headless sessions.
    result = subprocess.run(
        [sys.executable, "-m", "patchright", "install", "chromium", "--no-shell"],
        env=env,
        check=False,
    )
    return result.returncode


def verify_extension(context, manifest: dict) -> dict:
    from patchright.sync_api import TimeoutError as BrowserTimeoutError

    def matches(worker):
        parsed = urlparse(worker.url)
        return parsed.scheme == "chrome-extension" and parsed.path == "/" + manifest["background"]["service_worker"]

    worker = next((worker for worker in context.service_workers if matches(worker)), None)
    if worker is None:
        try:
            worker = context.wait_for_event("serviceworker", predicate=matches, timeout=30_000)
        except BrowserTimeoutError as exc:
            raise ExtensionError("Extension service worker did not start; extension test failed.") from exc
    actual = worker.evaluate(
        """() => ({
            id: chrome.runtime.id,
            name: chrome.i18n.getMessage('appName') || chrome.runtime.getManifest().name,
            version: chrome.runtime.getManifest().version,
            manifest_version: chrome.runtime.getManifest().manifest_version
        })""",
        isolated_context=False,
    )
    if (not isinstance(actual, dict) or actual.get("version") != manifest["version"]
            or actual.get("manifest_version") != 3 or actual.get("id") != urlparse(worker.url).netloc):
        raise ExtensionError("Loaded extension identity/version does not match its manifest.")
    actual["service_worker"] = worker.url
    return actual


def resolve_browser_proxy(raw_proxy) -> dict[str, str] | None:
    raw_proxy = str(raw_proxy or "").strip()
    if raw_proxy.casefold() in {"", "none", "off", "direct", "disable", "disabled", "false", "0"}:
        return None
    if raw_proxy.casefold().startswith("socks5h://"):
        raw_proxy = "socks5://" + raw_proxy[len("socks5h://") :]
    parsed_proxy = urlparse(raw_proxy)
    if parsed_proxy.scheme.casefold() not in {"http", "https", "socks5"} or not parsed_proxy.netloc:
        raise ExtensionError("PATCHRIGHT_PROXY must be http://, https://, or socks5://HOST:PORT.")
    return {"server": raw_proxy}


def open_browser(args: argparse.Namespace) -> int:
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_DIR)
    from patchright.sync_api import sync_playwright

    raw_proxy = args.proxy if args.proxy is not None else config_value("PATCHRIGHT_PROXY", "")
    browser_proxy = resolve_browser_proxy(raw_proxy)
    launch_args = [] if browser_proxy else ["--no-proxy-server"]
    with ExitStack() as stack:
        extension_dir = args.extension_dir.resolve() if args.extension_dir else None
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
        playwright = stack.enter_context(sync_playwright())
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(args.profile_dir.resolve()),
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
                print(json.dumps({"extension": extension}, ensure_ascii=True), flush=True)
            page = context.pages[0] if context.pages else context.new_page()
            response = page.goto(args.url, wait_until="domcontentloaded", timeout=90_000)
            page.locator("body").wait_for(state="attached", timeout=30_000)
            if args.screenshot:
                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(args.screenshot), full_page=True, timeout=30_000)
            print(
                json.dumps(
                    {
                        "url": page.url,
                        "title": page.title(),
                        "http_status": response.status if response else None,
                        "browser_version": context.browser.version,
                        "executable": playwright.chromium.executable_path,
                        "profile_dir": str(args.profile_dir.resolve()),
                        "proxy": "configured" if browser_proxy else "direct",
                        "screenshot": str(args.screenshot.resolve()) if args.screenshot else None,
                        "extension": extension,
                    },
                    ensure_ascii=True,
                ),
                flush=True,
            )
            if args.check:
                return 0 if response is None or response.ok else 1
            context.wait_for_event("close", timeout=0)
            return 0
        finally:
            context.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "install":
        return install_browser()
    try:
        return open_browser(args)
    except ExtensionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
