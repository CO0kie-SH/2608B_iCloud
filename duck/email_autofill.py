"""Gate DuckDuckGo Email Autofill access on the mayips egress country."""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from .duck_browser import BROWSERS_DIR, EXTENSION_DIR, PROFILE_DIR

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAYIPS_URL = "https://mayips.com/"
AUTOFILL_URL = "https://duckduckgo.com/email/settings/autofill"
BLOCKED_COUNTRY = "CN"
BLOCKED_MESSAGE = "中国出口无法访问"


class EgressBlockedError(RuntimeError):
    """The egress country is blocked for the next page in this flow."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-dir", type=Path, default=PROFILE_DIR)
    parser.add_argument("--proxy", default=None, help="HTTP/HTTPS/SOCKS5 proxy; defaults to PATCHRIGHT_PROXY")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--wait-ms", type=int, default=7000, help="Wait after Autofill navigation")
    return parser


def parse_mayips_payload(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise EgressBlockedError("无法读取出口信息") from exc
    if not isinstance(payload, dict):
        raise EgressBlockedError("无法读取出口信息")
    return payload


def check_country(payload: dict[str, Any]) -> str:
    country = str(payload.get("country") or "").strip().upper()
    if country == BLOCKED_COUNTRY:
        raise EgressBlockedError(BLOCKED_MESSAGE)
    if not country:
        raise EgressBlockedError("无法读取出口国家")
    return country


def run(args: argparse.Namespace) -> dict[str, Any]:
    from patchright.sync_api import sync_playwright

    from scripts.patchright_browser import resolve_browser_proxy, verify_extension
    from tools.config import config_value
    from tools.duckduckgo_extension import ExtensionLock, browser_version, validate_manifest

    if args.wait_ms < 0:
        raise ValueError("--wait-ms must be >= 0")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_DIR)
    raw_proxy = args.proxy if args.proxy is not None else config_value("PATCHRIGHT_PROXY", "")
    browser_proxy = resolve_browser_proxy(raw_proxy)
    extension_dir = EXTENSION_DIR.resolve()
    manifest = validate_manifest(extension_dir, browser_version())
    launch_args = [
        f"--disable-extensions-except={extension_dir}",
        f"--load-extension={extension_dir}",
    ]
    if browser_proxy is None:
        launch_args.insert(0, "--no-proxy-server")

    with ExitStack() as stack:
        stack.enter_context(ExtensionLock(extension_dir))
        playwright = stack.enter_context(sync_playwright())
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(args.profile_dir.resolve()),
            channel="chromium",
            headless=args.headless,
            no_viewport=True,
            proxy=browser_proxy,
            args=launch_args,
        )
        stack.callback(context.close)
        extension = verify_extension(context, manifest)
        page = context.pages[0] if context.pages else context.new_page()

        first_response = page.goto(MAYIPS_URL, wait_until="domcontentloaded", timeout=90_000)
        page.locator("body").wait_for(state="attached", timeout=30_000)
        payload = parse_mayips_payload(page.locator("body").inner_text(timeout=30_000))
        country = check_country(payload)

        autofill = context.new_page()
        second_response = autofill.goto(AUTOFILL_URL, wait_until="domcontentloaded", timeout=90_000)
        autofill.locator("body").wait_for(state="attached", timeout=30_000)
        try:
            autofill.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            pass
        if args.wait_ms:
            autofill.wait_for_timeout(args.wait_ms)
        result = {
            "country": country,
            "egress": payload,
            "mayips_status": first_response.status if first_response else None,
            "autofill_status": second_response.status if second_response else None,
            "autofill_url": autofill.url,
            "autofill_title": autofill.title(),
            "autofill_text": autofill.locator("body").inner_text(timeout=30_000),
            "extension": extension,
            "proxy": "configured" if browser_proxy else "direct",
        }
        if not args.headless:
            context.wait_for_event("close", timeout=0)
        return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        print(json.dumps(run(args), ensure_ascii=False, indent=2), flush=True)
        return 0
    except EgressBlockedError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr, flush=True)
        return 130
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
