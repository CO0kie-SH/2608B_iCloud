from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import yaml

from .accounts import Account
from .camoufox_runtime import CamoufoxRuntimeError, ensure_browser
from .config import Settings
from .cookies import (
    REQUIRED_COOKIE_KEYS,
    cookie_header_from_dict,
    missing_required_keys,
    parse_cookie_keys,
)
from .logging_setup import cookie_keys_summary

log = logging.getLogger("2608b.cookie_login")

# 登录阶段（日志用；成功只以必填 cookie 为准）
STAGE_LAUNCH = "launch"
STAGE_LANDING = "landing"
STAGE_LOGIN_FORM = "login_form"
STAGE_2FA = "2fa_challenge"
STAGE_LOGGED_IN = "logged_in"
STAGE_COOKIE_READY = "cookie_ready"
STAGE_DONE = "done"
STAGE_TIMEOUT = "timeout"
STAGE_ERROR = "error"


@dataclass
class CookieCaptureOptions:
    """有头采集选项；headless 预留给后续无头自动化。"""

    headless: bool = False  # 本期强制 False；True 时仅警告仍走有头
    timeout_sec: float = 600.0
    poll_interval_sec: float = 2.0
    url: str | None = None
    humanize: bool = True
    os_name: str = "windows"
    # 后续无头：收到 2FA 时回调（本期不用）
    on_2fa: Callable[[dict[str, Any]], None] | None = None
    backup: bool = True


@dataclass
class CaptureResult:
    ok: bool
    stage: str
    cookie_header: str = ""
    keys: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    account_path: str = ""
    message: str = ""


def _default_url(settings: Settings) -> str:
    return f"{settings.origin}/"


def playwright_cookies_to_header(
    cookies: list[dict[str, Any]],
    *,
    domain_hints: tuple[str, ...] = ("icloud", "apple", "mzstatic"),
) -> str:
    """Playwright context.cookies() → Cookie 请求头字符串。"""
    picked: dict[str, str] = {}
    for c in cookies:
        name = str(c.get("name") or "").strip()
        value = c.get("value")
        if not name or value is None:
            continue
        domain = str(c.get("domain") or "").lower()
        if domain_hints and not any(h in domain for h in domain_hints):
            # 仍保留 X-APPLE-* 名（有时 domain 怪异）
            if not name.upper().startswith("X-APPLE") and "APPLE" not in name.upper():
                continue
        picked[name] = str(value)
    return cookie_header_from_dict(picked)


def detect_stage(*, url: str, title: str, cookie_header: str, missing: list[str]) -> str:
    if not missing and cookie_header:
        return STAGE_COOKIE_READY

    u = (url or "").lower()
    t = (title or "").lower()
    blob = f"{u} {t}"

    keys = set(parse_cookie_keys(cookie_header)) if cookie_header else set()
    has_login_token = "X-APPLE-WEBAUTH-LOGIN" in keys
    has_session = "X-APPLE-DS-WEB-SESSION-TOKEN" in keys

    two_fa_hints = (
        "two-factor",
        "twofactor",
        "2fa",
        "authcode",
        "auth-code",
        "verify",
        "verification",
        "hsa2",
        "secondfactor",
        "双重",
        "验证码",
        "两步",
        "双因素",
    )
    login_hints = ("signin", "login", "idmsa.apple.com", "appleid.apple.com/auth")
    app_hints = ("icloud.com/", "/mail", "/contacts", "/iclouddrive", "applications")

    if any(h in blob for h in two_fa_hints) and not (has_login_token and has_session):
        return STAGE_2FA
    if any(h in blob for h in login_hints):
        return STAGE_LOGIN_FORM
    if has_login_token or has_session:
        return STAGE_LOGGED_IN
    if any(h in u for h in app_hints) and "signin" not in u:
        return STAGE_LANDING
    return STAGE_LANDING


def update_account_cookie(path: Path, cookie_header: str, *, backup: bool = True) -> str:
    """
    写回账户 YAML：
      - 有 apple: 映射 → apple.cookie
      - 否则根级 cookie
    返回写入位置描述。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"账户文件不存在: {path}")

    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"账户 YAML 根节点必须是映射: {path}")

    if backup:
        bak = path.with_suffix(path.suffix + ".bak")
        bak.write_text(text, encoding="utf-8", newline="\n")

    where: str
    apple = data.get("apple")
    if isinstance(apple, dict):
        apple = dict(apple)
        apple["cookie"] = cookie_header
        data["apple"] = apple
        # 扁平残留 cookie 若存在可同步，避免双份不一致
        if "cookie" in data and not isinstance(data.get("cookie"), dict):
            data["cookie"] = cookie_header
        where = "apple.cookie"
    else:
        data["cookie"] = cookie_header
        where = "cookie"

    dumped = yaml.safe_dump(
        data,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=10_000,
    )
    path.write_text(dumped, encoding="utf-8", newline="\n")
    return where


def capture_icloud_cookie(
    settings: Settings,
    account: Account,
    options: CookieCaptureOptions | None = None,
    *,
    logger: logging.Logger | None = None,
) -> CaptureResult:
    """
    有头打开 iCloud，等待用户登录（含浏览器内 2FA），采集 cookie 写回账户 YAML。
    """
    lg = logger or log
    opts = options or CookieCaptureOptions()
    account_path = Path(account.source)

    if opts.headless:
        lg.warning(
            "stage=config headless=True 被忽略：本期仅支持有头；后续无头自动化另做"
        )
        opts.headless = False

    url = opts.url or _default_url(settings)
    lg.info(
        "stage=%s account=%s path=%s url=%s timeout=%ss",
        STAGE_LAUNCH,
        account.name,
        account_path.name,
        url,
        int(opts.timeout_sec),
    )
    lg.info(
        "hint=请在弹出的浏览器中完成 Apple 登录；"
        "若出现手机验证码/2FA，直接在浏览器页面输入（不要在终端输入验证码）"
    )

    try:
        exe = ensure_browser(settings, fetch_if_missing=False)
    except CamoufoxRuntimeError as e:
        lg.error("stage=%s %s", STAGE_ERROR, e)
        return CaptureResult(ok=False, stage=STAGE_ERROR, message=str(e))

    try:
        from camoufox.sync_api import Camoufox
    except ImportError:
        msg = "未安装 camoufox，请 pip install -U \"camoufox[geoip]\""
        lg.error("stage=%s %s", STAGE_ERROR, msg)
        return CaptureResult(ok=False, stage=STAGE_ERROR, message=msg)

    # 确保 launch 仍用项目内 INSTALL_DIR；并清掉下载代理，避免浏览器走 7897
    from .camoufox_runtime import (
        browser_launch_env,
        clear_proxy_env,
        configure_install_dir,
        resolve_camoufox_dir,
    )

    configure_install_dir(resolve_camoufox_dir(settings))
    clear_proxy_env()
    lg.info("stage=proxy_policy browser=direct (no CAMOUFOX_PROXY for page traffic)")

    last_stage = STAGE_LAUNCH
    cookie_header = ""
    missing = list(REQUIRED_COOKIE_KEYS)
    keys: list[str] = []

    deadline = time.monotonic() + max(30.0, float(opts.timeout_sec))

    try:
        launch_kwargs: dict[str, Any] = {
            "headless": False,
            "humanize": opts.humanize,
            "os": opts.os_name,
            "executable_path": exe,
            # 不给浏览器套下载代理；中国区 icloud.com.cn 直连
            "proxy": None,
            "env": browser_launch_env(),
        }
        # 避免首次启动再去下 UBO（慢/易失败）；采集 cookie 不需要广告过滤
        try:
            from camoufox.addons import DefaultAddons

            launch_kwargs["exclude_addons"] = [DefaultAddons.UBO]
        except Exception:
            pass

        with Camoufox(**launch_kwargs) as browser:
            # NewBrowser 默认返回 Browser
            context = browser.new_context()
            page = context.new_page()
            lg.info("stage=%s goto=%s", STAGE_LANDING, url)
            page.goto(url, wait_until="domcontentloaded", timeout=120_000)

            while time.monotonic() < deadline:
                try:
                    page_url = page.url or ""
                    title = page.title() or ""
                except Exception as e:
                    lg.debug("page meta error: %s", e)
                    page_url, title = "", ""

                try:
                    raw_cookies = context.cookies()
                except Exception as e:
                    lg.debug("cookies() error: %s", e)
                    raw_cookies = []

                cookie_header = playwright_cookies_to_header(raw_cookies)
                keys = parse_cookie_keys(cookie_header)
                missing = missing_required_keys(cookie_header)
                stage = detect_stage(
                    url=page_url,
                    title=title,
                    cookie_header=cookie_header,
                    missing=missing,
                )

                if stage != last_stage:
                    host = urlparse(page_url).netloc or "-"
                    lg.info(
                        "stage=%s host=%s title_len=%s keys=%d missing=%s key_names=%s",
                        stage,
                        host,
                        len(title),
                        len(keys),
                        ",".join(missing) if missing else "-",
                        cookie_keys_summary(keys),
                    )
                    if stage == STAGE_2FA:
                        lg.info(
                            "stage=%s action=waiting_user_input "
                            "msg=请在浏览器中输入手机验证码/完成双重认证",
                            STAGE_2FA,
                        )
                        if opts.on_2fa:
                            # 预留无头钩子
                            opts.on_2fa(
                                {
                                    "url": page_url,
                                    "title": title,
                                    "keys": keys,
                                    "missing": missing,
                                }
                            )
                    last_stage = stage

                if stage == STAGE_COOKIE_READY:
                    lg.info("stage=%s required_cookies_present", STAGE_COOKIE_READY)
                    break

                time.sleep(max(0.5, float(opts.poll_interval_sec)))
            else:
                lg.error(
                    "stage=%s missing=%s keys=%d",
                    STAGE_TIMEOUT,
                    ",".join(missing) if missing else "-",
                    len(keys),
                )
                return CaptureResult(
                    ok=False,
                    stage=STAGE_TIMEOUT,
                    cookie_header=cookie_header,
                    keys=keys,
                    missing=missing,
                    account_path=str(account_path),
                    message="等待登录超时，必填 cookie 仍不完整",
                )

            try:
                context.close()
            except Exception:
                pass

    except Exception as e:
        lg.exception("stage=%s error=%s", STAGE_ERROR, e)
        return CaptureResult(
            ok=False,
            stage=STAGE_ERROR,
            message=f"{type(e).__name__}: {e}",
            account_path=str(account_path),
        )

    missing = missing_required_keys(cookie_header)
    if missing:
        lg.error("stage=%s missing=%s", STAGE_ERROR, ",".join(missing))
        return CaptureResult(
            ok=False,
            stage=STAGE_ERROR,
            cookie_header=cookie_header,
            keys=keys,
            missing=missing,
            account_path=str(account_path),
            message=f"cookie 缺必填键: {', '.join(missing)}",
        )

    try:
        where = update_account_cookie(
            account_path, cookie_header, backup=opts.backup
        )
    except Exception as e:
        lg.exception("stage=%s write_failed %s", STAGE_ERROR, e)
        return CaptureResult(
            ok=False,
            stage=STAGE_ERROR,
            cookie_header=cookie_header,
            keys=keys,
            missing=[],
            account_path=str(account_path),
            message=f"写回 YAML 失败: {e}",
        )

    lg.info(
        "stage=%s wrote=%s file=%s keys=%d key_names=%s",
        STAGE_DONE,
        where,
        account_path.name,
        len(keys),
        cookie_keys_summary(keys),
    )
    return CaptureResult(
        ok=True,
        stage=STAGE_DONE,
        cookie_header=cookie_header,
        keys=keys,
        missing=[],
        account_path=str(account_path),
        message=f"已写入 {where}",
    )


__all__ = [
    "CaptureResult",
    "CookieCaptureOptions",
    "capture_icloud_cookie",
    "detect_stage",
    "playwright_cookies_to_header",
    "update_account_cookie",
]
