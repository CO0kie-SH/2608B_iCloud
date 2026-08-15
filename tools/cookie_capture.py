from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
from .session_store import (
    inject_session,
    load_session,
    playwright_cookies_to_records,
    save_page_snapshot,
    save_session,
    session_file,
)

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
    keep_open_sec: float = 0.0
    debug: bool = False
    debug_dir: Path | None = None
    debug_max_html: int = 1_500_000
    reuse_session: bool = True
    follow_appleid: bool = False
    appleid_url: str = ""


@dataclass
class CaptureResult:
    ok: bool
    stage: str
    cookie_header: str = ""
    keys: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    account_path: str = ""
    message: str = ""
    debug_dir: str = ""
    debug_pages: int = 0


def _default_url(settings: Settings) -> str:
    return f"{settings.origin}/"


APP_PASSWORD_RE = re.compile(r"\b([a-z]{4}-[a-z]{4}-[a-z]{4}-[a-z]{4})\b", re.I)
APP_PASSWORD_HINTS = (
    "专用密码",
    "app-specific",
    "app specific password",
    "app password",
    "生成密码",
    "app-specific password",
)


def _safe_name(value: str, max_len: int = 48) -> str:
    text = re.sub(r"[^\w.@+-]+", "_", (value or "").strip())
    return (text[:max_len] if text else "page")


def _frame_blob(frame: Any) -> tuple[str, str, str]:
    url = ""
    html = ""
    text = ""
    try:
        url = frame.url or ""
    except Exception:
        pass
    try:
        html = frame.content() or ""
    except Exception:
        pass
    try:
        text = frame.inner_text("body") or ""
    except Exception:
        try:
            text = frame.evaluate("() => document.body ? document.body.innerText : ''") or ""
        except Exception:
            pass
    return url, html, text


def extract_app_passwords(*blobs: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    joined = "\n".join(b or "" for b in blobs)
    hinted = any(h in joined.lower() for h in APP_PASSWORD_HINTS) or "专用密码" in joined
    for match in APP_PASSWORD_RE.findall(joined):
        pwd = match.lower()
        if pwd in seen:
            continue
        # 没提示词时也收：Apple 弹窗经常只有四段密码
        seen.add(pwd)
        found.append(pwd)
    if found and not hinted:
        return found
    return found


def update_account_app_password(path: Path, password: str, *, backup: bool = True) -> str:
    """写回 apple.app_password；有扁平 app_password 时一并同步。"""
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
    apple = data.get("apple")
    if isinstance(apple, dict):
        apple = dict(apple)
        apple["app_password"] = password
        data["apple"] = apple
        if "app_password" in data and not isinstance(data.get("app_password"), dict):
            data["app_password"] = password
        where = "apple.app_password"
    else:
        data["app_password"] = password
        where = "app_password"
    dumped = yaml.safe_dump(
        data,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=10_000,
    )
    path.write_text(dumped, encoding="utf-8", newline="\n")
    return where


class PageDebugDumper:
    """按页面内容变化落盘；同一 URL 里弹窗/iframe 变了也会再采。"""

    def __init__(
        self,
        root: Path,
        *,
        max_html: int = 1_500_000,
        account_path: Path | None = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_html = max(50_000, int(max_html))
        self.account_path = Path(account_path) if account_path else None
        self.index_path = self.root / "index.jsonl"
        self.seen: set[str] = set()
        self.count = 0
        self.app_passwords: list[str] = []

    def maybe_dump(self, page: Any, *, stage: str, lg: logging.Logger) -> Path | None:
        try:
            url = page.url or ""
            title = page.title() or ""
        except Exception as exc:
            lg.debug("debug dump meta failed: %s", exc)
            return None
        html = ""
        text = ""
        try:
            html = page.content() or ""
        except Exception as exc:
            lg.debug("debug dump html failed: %s", exc)
        try:
            text = page.inner_text("body") or ""
        except Exception:
            try:
                text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
            except Exception as exc:
                lg.debug("debug dump text failed: %s", exc)

        frames: list[dict[str, str]] = []
        try:
            for frame in page.frames:
                if frame == getattr(page, "main_frame", None):
                    continue
                furl, fhtml, ftext = _frame_blob(frame)
                if not furl and not fhtml and not ftext:
                    continue
                frames.append({"url": furl, "html": fhtml, "text": ftext})
        except Exception as exc:
            lg.debug("debug dump frames failed: %s", exc)

        digest_src = "\n".join(
            [url, title, html, text] + [f"{f['url']}\n{f['html']}\n{f['text']}" for f in frames]
        )
        key = hashlib.sha1(digest_src.encode("utf-8", "replace")).hexdigest()
        if key in self.seen:
            return None
        self.seen.add(key)
        self.count += 1
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        digest = key[:8]
        stem = f"{self.count:03d}-{stamp}-{digest}-{_safe_name(title or urlparse(url).netloc)}"
        truncated = len(html) > self.max_html
        if truncated:
            html = html[: self.max_html]
        html_path = self.root / f"{stem}.html"
        text_path = self.root / f"{stem}.txt"
        meta_path = self.root / f"{stem}.json"
        html_path.write_text(html, encoding="utf-8", errors="replace")
        text_path.write_text(text, encoding="utf-8", errors="replace")
        frame_files: list[dict[str, str]] = []
        for idx, frame in enumerate(frames, start=1):
            fhtml = frame["html"]
            if len(fhtml) > self.max_html:
                fhtml = fhtml[: self.max_html]
            f_html_path = self.root / f"{stem}-frame{idx}.html"
            f_text_path = self.root / f"{stem}-frame{idx}.txt"
            f_html_path.write_text(fhtml, encoding="utf-8", errors="replace")
            f_text_path.write_text(frame["text"], encoding="utf-8", errors="replace")
            frame_files.append(
                {
                    "url": frame["url"],
                    "html": f_html_path.name,
                    "text": f_text_path.name,
                }
            )

        passwords = extract_app_passwords(
            text,
            html,
            *[frame["text"] for frame in frames],
            *[frame["html"] for frame in frames],
        )
        wrote_password = ""
        for pwd in passwords:
            if pwd not in self.app_passwords:
                self.app_passwords.append(pwd)
            if self.account_path:
                try:
                    wrote_password = update_account_app_password(self.account_path, pwd)
                    lg.info("stage=app_password_found wrote=%s value=%s", wrote_password, pwd)
                except Exception as exc:
                    lg.error("stage=app_password_write_failed %s", exc)

        meta = {
            "n": self.count,
            "utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "stage": stage,
            "url": url,
            "title": title,
            "content_sha1": key,
            "html_bytes": html_path.stat().st_size,
            "text_bytes": text_path.stat().st_size,
            "truncated": truncated,
            "html": html_path.name,
            "text": text_path.name,
            "frames": frame_files,
            "app_passwords": passwords,
            "app_password_wrote": wrote_password,
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        with self.index_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(meta, ensure_ascii=False) + "\n")
        lg.info(
            "stage=debug_dump n=%s url=%s title_len=%s html=%s frames=%s passwords=%s",
            self.count,
            url[:180],
            len(title),
            html_path.name,
            len(frame_files),
            ",".join(passwords) if passwords else "-",
        )
        return html_path

    def dump_context(self, context: Any, *, stage: str, lg: logging.Logger) -> None:
        try:
            pages = list(context.pages)
        except Exception as exc:
            lg.debug("debug dump context pages failed: %s", exc)
            return
        for item in pages:
            self.maybe_dump(item, stage=stage, lg=lg)


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
    dumper: PageDebugDumper | None = None
    if opts.debug:
        debug_root = Path(opts.debug_dir) if opts.debug_dir else None
        if debug_root is None:
            raw = (getattr(settings, "log_dir", None) or "logs").strip()
            base = Path(raw)
            if not base.is_absolute():
                base = settings.base_dir / base
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            debug_root = base / f"page-debug-{_safe_name(account.name)}-{stamp}"
        dumper = PageDebugDumper(
            debug_root,
            max_html=opts.debug_max_html,
            account_path=account_path,
        )
        lg.info("stage=debug_on dir=%s watch=content+iframe+popup", dumper.root)

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
    last_session_sig = ""

    deadline = time.monotonic() + max(30.0, float(opts.timeout_sec))

    def persist_browser_cookies(ctx: Any, *, page_url: str = "", title: str = "", stage: str = "") -> None:
        nonlocal last_session_sig
        try:
            raw = ctx.cookies()
        except Exception as exc:
            lg.debug("session cookies() failed: %s", exc)
            return
        records = playwright_cookies_to_records(raw)
        if not records:
            return
        sig = "|".join(
            f"{c.get('domain','')}:{c.get('name','')}={c.get('value','')}"
            for c in records
        )
        if sig == last_session_sig:
            return
        last_session_sig = sig
        try:
            path = save_session(settings, account.name, records, source_url=page_url)
            lg.info("stage=session_save file=%s cookies=%d", path.name, len(records))
        except Exception as exc:
            lg.warning("stage=session_save_failed %s", exc)
        if opts.debug:
            try:
                snap = save_page_snapshot(
                    settings,
                    account.name,
                    records,
                    url=page_url,
                    title=title,
                    stage=stage,
                )
                lg.info("stage=session_snap file=%s", snap.name)
            except Exception as exc:
                lg.debug("session snapshot failed: %s", exc)

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
            if opts.reuse_session:
                stored = load_session(settings, account.name)
                if stored:
                    try:
                        injected = inject_session(context, stored)
                        lg.info(
                            "stage=session_inject file=%s cookies=%d",
                            session_file(settings, account.name).name,
                            injected,
                        )
                    except Exception as exc:
                        lg.warning("stage=session_inject_failed %s", exc)
            page = context.new_page()
            lg.info("stage=%s goto=%s", STAGE_LANDING, url)
            page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            if dumper:
                dumper.dump_context(context, stage=STAGE_LANDING, lg=lg)

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
                    if dumper:
                        dumper.dump_context(context, stage=stage, lg=lg)
                elif dumper:
                    dumper.dump_context(context, stage=stage, lg=lg)
                persist_browser_cookies(context, page_url=page_url, title=title, stage=stage)

                if stage == STAGE_COOKIE_READY:
                    lg.info("stage=%s required_cookies_present", STAGE_COOKIE_READY)
                    if opts.follow_appleid and (opts.appleid_url or "").strip():
                        dest = opts.appleid_url.strip()
                        lg.info("stage=follow_appleid goto=%s", dest)
                        try:
                            page.goto(dest, wait_until="domcontentloaded", timeout=120_000)
                        except Exception as exc:
                            lg.warning("stage=follow_appleid_failed %s", exc)
                        if dumper:
                            dumper.dump_context(context, stage="follow_appleid", lg=lg)
                        persist_browser_cookies(
                            context,
                            page_url=page.url or dest,
                            title=page.title() if hasattr(page, "title") else "",
                            stage="follow_appleid",
                        )
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
                    debug_dir=str(dumper.root) if dumper else "",
                    debug_pages=dumper.count if dumper else 0,
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
                    debug_dir=str(dumper.root) if dumper else "",
                    debug_pages=dumper.count if dumper else 0,
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
                    debug_dir=str(dumper.root) if dumper else "",
                    debug_pages=dumper.count if dumper else 0,
                )

            lg.info(
                "stage=%s wrote=%s file=%s keys=%d key_names=%s",
                STAGE_DONE,
                where,
                account_path.name,
                len(keys),
                cookie_keys_summary(keys),
            )

            keep_open = max(0.0, float(opts.keep_open_sec or 0.0))
            if keep_open > 0:
                lg.info(
                    "stage=keep_open hold=%ss msg=已写回 YAML，浏览器继续开着",
                    int(keep_open),
                )
                hold_until = time.monotonic() + keep_open
                while time.monotonic() < hold_until:
                    left = int(hold_until - time.monotonic())
                    if left % 10 == 0 or left <= 5:
                        lg.info("stage=keep_open remaining=%ss", max(left, 0))
                    if dumper:
                        dumper.dump_context(context, stage="keep_open", lg=lg)
                    try:
                        hold_title = page.title() or ""
                    except Exception:
                        hold_title = ""
                    persist_browser_cookies(
                        context,
                        page_url=getattr(page, "url", "") or "",
                        title=hold_title,
                        stage="keep_open",
                    )
                    time.sleep(1.0)

            persist_browser_cookies(context, page_url=url, stage="done")
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

    return CaptureResult(
        ok=True,
        stage=STAGE_DONE,
        cookie_header=cookie_header,
        keys=keys,
        missing=[],
        account_path=str(account_path),
        message=f"已写入 {where}",
        debug_dir=str(dumper.root) if dumper else "",
        debug_pages=dumper.count if dumper else 0,
    )


__all__ = [
    "CaptureResult",
    "CookieCaptureOptions",
    "capture_icloud_cookie",
    "detect_stage",
    "playwright_cookies_to_header",
    "update_account_cookie",
    "update_account_app_password",
    "extract_app_passwords",
]
