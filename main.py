from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tools.accounts import (
    create_account_file,
    find_account,
    load_all_accounts,
    parse_accounts_file,
)
from tools.camoufox_runtime import (
    CamoufoxRuntimeError,
    browser_status,
    fetch_browser,
    resolve_camoufox_dir,
)
from tools.client import CookieInvalidError, ICloudError, ICloudHMEClient, is_cookie_failure
from tools.config import add_region_arguments, apply_cli_domain, load_settings
from tools.cookie_capture import CookieCaptureOptions, capture_icloud_cookie
from tools.db import AliasDB
from tools.hme import HMEService
from tools.logging_setup import setup_logger
from tools.mail import ICloudMailClient, get_mail_by_uid, mail_client_from_account
from tools.rate_limit import (
    HME_CREATE_LIMIT_PER_HOUR,
    HME_CREATE_MAX_INTERVAL_MINUTES,
    HME_CREATE_MIN_INTERVAL_MINUTES,
    HMECreateRateLimitError,
)


def get_db() -> AliasDB:
    settings = load_settings()
    return AliasDB(settings.base_dir / "db" / "aliases.db")


def cmd_accounts(args: argparse.Namespace) -> int:
    settings = apply_cli_domain(load_settings(), args)
    files, accounts = load_all_accounts(settings.accounts_files, settings.base_dir)

    print(f"{settings.app_name} (domain={settings.domain})")
    print(f"source files ({len(files)}):")
    for f in files:
        flag = "" if f.exists() else " (missing)"
        print(f"  - {f}{flag}")
    print(f"loaded {len(accounts)} account(s)")
    db = get_db()
    for a in accounts:
        print(f"  - {a.summary()}")
        if a.format_errors:
            print(f"    format: {'; '.join(a.format_errors)}")
        prov_names = ", ".join(sorted(a.providers.keys())) or "-"
        inbox = a.resolve_inbox()
        inbox_s = f"{inbox.name}:{inbox.mail}" if inbox and inbox.mail else "-"
        apple_s = a.apple_id or "-"
        flag = db.get_account_flag(a.name) or {}
        marked = bool(flag.get("cookie_invalid"))
        print(
            f"    mail={a.mail or '-'}  appleid={apple_s}  "
            f"providers=[{prov_names}]  inbox={inbox_s}  "
            f"hme_ok={a.ok and not marked}  mail_ready={a.mail_ready}  "
            f"cookie_invalid={marked}"
        )
        if settings.debug and a.cookies:
            preview = a.cookies[:60] + ("..." if len(a.cookies) > 60 else "")
            print(f"    cookie: {preview}")
    return 0


def _pick_account(name: str | None, args: argparse.Namespace | None = None):
    settings = apply_cli_domain(load_settings(), args)
    _, accounts = load_all_accounts(settings.accounts_files, settings.base_dir)
    if not accounts:
        raise SystemExit("未加载到任何账户，请检查 accounts/ 与 .env")

    if name:
        acc = find_account(accounts, name)
        if not acc:
            names = ", ".join(a.name for a in accounts)
            raise SystemExit(f"未找到账户: {name}（可用: {names}）")
        return settings, acc

    if len(accounts) == 1:
        return settings, accounts[0]

    names = ", ".join(a.name for a in accounts)
    raise SystemExit(f"存在多个账户，请用 --account 指定（可用: {names}）")


def _pick_or_create_cookie_account(
    name: str | None,
    args: argparse.Namespace,
):
    """cookie-login 指定新邮箱时，创建最小账户文件后继续采集。"""
    if not name:
        settings, account = _pick_account(None, args)
        return settings, account, None

    settings = apply_cli_domain(load_settings(), args)
    _, accounts = load_all_accounts(settings.accounts_files, settings.base_dir)
    account = find_account(accounts, name)
    if account:
        return settings, account, None

    try:
        path, created = create_account_file(
            settings.accounts_files,
            settings.base_dir,
            name,
            domain=settings.domain,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    parsed = parse_accounts_file(path)
    if not parsed:
        raise SystemExit(f"账户文件创建后加载失败: {path}")
    return settings, parsed[0], path if created else None


def cmd_list(args: argparse.Namespace) -> int:
    settings, account = _pick_account(args.account, args)
    db = get_db()
    if db.is_cookie_invalid(account.name):
        print(f"[{account.name}] COOKIE_INVALID: 已标记失效，跳过 iCloud 拉取")
        return 3
    if not account.ok:
        print(f"警告: cookie 可能不完整 -> {account.summary()}")

    try:
        with ICloudHMEClient(settings, account.cookies) as client:
            svc = HMEService(client)
            aliases = svc.list_aliases()
    except (ICloudError, RuntimeError) as e:
        if is_cookie_failure(e):
            db.mark_cookie_invalid(account.name, reason="http_421")
            print(f"[{account.name}] COOKIE_INVALID: {e}")
            return 3
        print(f"[{account.name}] 失败: {e}")
        return 1
    for a in aliases:
        db.upsert_alias(
            account=account.name,
            parent_mail=account.mail or account.name,
            hme=a.hme,
            label=a.label,
            cdk=a.label if str(a.label).startswith("CDK_") else None,
            anonymous_id=a.anonymous_id,
            is_active=a.is_active,
            create_timestamp=a.create_timestamp,
            source="list",
            raw=a.raw,
        )

    print(f"[{account.name}] {len(aliases)} alias(es)  (synced -> db/aliases.db)")
    for a in aliases:
        state = "ON" if a.is_active else "OFF"
        print(f"  [{state}] {a.hme}  label={a.label}  id={a.anonymous_id}")
    return 0


def cmd_db(args: argparse.Namespace) -> int:
    db = get_db()
    rows = db.list_aliases(account=args.account)
    if not rows:
        print("db 中无记录")
        return 0
    print(f"db records: {len(rows)}  ({db.db_path})")
    for r in rows:
        state = "ON" if r.is_active else "OFF"
        print(
            f"  #{r.id} [{state}] cdk={r.cdk or '-'}  hme={r.hme}  "
            f"parent={r.parent_mail or r.account}  id={r.anonymous_id}  at={r.created_at}"
        )
    return 0


def cmd_cdk(args: argparse.Namespace) -> int:
    """通过 CDK 查询 隐私邮箱 + 母号。"""
    import json

    db = get_db()
    if args.list_map:
        m = db.cdk_map(parent_mail=args.account)
        print(json.dumps(m, ensure_ascii=False, indent=2))
        print(f"count={len(m)}")
        return 0

    if not args.cdk:
        print("请提供 --cdk CDK_xxx，或使用 --list-map")
        return 1

    data = db.resolve_cdk(args.cdk)
    if not data:
        print(f"未找到 CDK: {args.cdk}")
        return 1
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    settings, account = _pick_account(args.account, args)
    db = get_db()
    try:
        db.assert_cookie_ready(account.name)
        with ICloudHMEClient(settings, account.cookies) as client:
            svc = HMEService(client, db=db)
            alias = svc.create_alias(
                account=account.name,
                label=args.label,
                note=args.note,
                cdk_hex_len=args.cdk_hex_len,
            )
    except CookieInvalidError as e:
        print(f"[{account.name}] {e}")
        return 3
    except HMECreateRateLimitError as e:
        print(f"[{account.name}] 拒绝创建（规矩：1小时最多{HME_CREATE_LIMIT_PER_HOUR}个）: {e}")
        return 2
    except (ICloudError, RuntimeError, ValueError) as e:
        if is_cookie_failure(e):
            db.mark_cookie_invalid(account.name, reason="http_421")
            print(f"[{account.name}] COOKIE_INVALID: {e}")
            return 3
        print(f"[{account.name}] 生成失败: {e}")
        return 1

    print(f"[{account.name}] 生成成功: {alias.hme}")
    print(f"  label/cdk={alias.label}")
    print(f"  parent_mail={account.name}")
    if alias.anonymous_id:
        print(f"  id={alias.anonymous_id}")
    # 展示 CDK 定位结果
    resolved = db.resolve_cdk(alias.label)
    if resolved:
        print(
            f"  map: {resolved.get('cdk')} -> "
            f"hme={resolved.get('hme')} parent={resolved.get('parent_mail')}"
        )
    q = db.get_create_quota(account.name)
    print(
        f"  quota: {q.used}/{q.limit} in 1h, remaining={q.remaining} "
        f"next_produce_at={q.next_produce_at}"
    )
    print(f"  saved -> {db.db_path}")
    return 0


def cmd_quota(args: argparse.Namespace) -> int:
    db = get_db()
    settings = load_settings()
    _, accounts = load_all_accounts(settings.accounts_files, settings.base_dir)
    targets = accounts
    if args.account:
        acc = find_account(accounts, args.account)
        if not acc:
            print(f"未找到账户: {args.account}")
            return 1
        targets = [acc]

    print(
        f"规矩: 每账户滚动1小时最多创建 {HME_CREATE_LIMIT_PER_HOUR} 个；"
        f"两次生产间隔 {HME_CREATE_MIN_INTERVAL_MINUTES}-{HME_CREATE_MAX_INTERVAL_MINUTES} 分钟"
    )
    for acc in targets:
        q = db.get_create_quota(acc.name)
        print(
            f"[{acc.name}] used={q.used}/{q.limit} remaining={q.remaining} "
            f"retry_after={q.retry_after_sec}s last={q.last_produce_at} next={q.next_produce_at}"
        )
        for ev in q.recent:
            print(
                f"  - {ev['created_at']}  {ev['hme']}  label={ev['label']} "
                f"produce_at={ev.get('produce_at') or 0} next_produce_at={ev.get('next_produce_at') or 0}"
            )
    return 0


def cmd_toggle(args: argparse.Namespace) -> int:
    settings, account = _pick_account(args.account, args)
    active = args.action == "on"
    try:
        with ICloudHMEClient(settings, account.cookies) as client:
            svc = HMEService(client)
            res = svc.set_active(args.anonymous_id, active=active)
    except (ICloudError, RuntimeError) as e:
        print(f"[{account.name}] 操作失败: {e}")
        return 1

    ok = bool(res.get("success")) if isinstance(res, dict) else False
    if ok:
        get_db().set_active(account.name, args.anonymous_id, active)

    print(f"[{account.name}] {'恢复' if active else '停用'} {'成功' if ok else '失败'}: {args.anonymous_id}")
    if settings.debug:
        print(res)
    return 0 if ok else 1


def _mail_client(account):
    try:
        return mail_client_from_account(account)
    except ValueError as e:
        raise SystemExit(f"[{account.name}] 邮件凭证不完整，请检查收件 provider 与 inbox.mail ({e})") from e


def cmd_mail_probe(args: argparse.Namespace) -> int:
    _, account = _pick_account(args.account)
    client = _mail_client(account)
    endpoint = account.resolve_inbox()
    ep = f"{endpoint.name}:{endpoint.mail}" if endpoint else "-"
    print(
        f"[{account.name}] account_mail={account.mail} "
        f"inbox={ep} provider={client.profile.name} "
        f"imap={client.profile.imap_host}"
    )
    imap = client.probe_imap()
    smtp = client.probe_smtp()
    print(f"  IMAP: {'OK' if imap.ok else 'FAIL'} - {imap.detail}")
    print(f"  SMTP: {'OK' if smtp.ok else 'FAIL'} - {smtp.detail}")
    # 收件以 IMAP 为准；SMTP 失败多为出站端口限制，单独警告
    if imap.ok and not smtp.ok:
        print("  note: IMAP 可用（收件 OK）；SMTP 失败不影响收信")
    return 0 if imap.ok else 1


def cmd_mail_inbox(args: argparse.Namespace) -> int:
    _, account = _pick_account(args.account)
    client = _mail_client(account)
    try:
        items = client.list_recent(
            limit=args.limit,
            mailbox=args.box,
            include_body=not args.no_body,
            body_limit=args.body_limit,
        )
    except Exception as e:
        print(f"[{account.name}] 读取失败: {e}")
        return 1

    print(f"[{account.name}] mailbox={args.box} count={len(items)}")
    for i, m in enumerate(items, 1):
        print("=" * 60)
        print(f"#{i}")
        print(f"  mailbox      : {m.get('mailbox')}")
        print(f"  seq          : {m.get('seq')}")
        print(f"  uid          : {m.get('uid')}")
        print(f"  flags        : {m.get('flags')}")
        print(f"  size         : {m.get('size')}")
        print(f"  date(send)   : {m.get('date')}")
        print(f"  date_parsed  : {m.get('date_parsed')}")
        print(f"  internaldate : {m.get('internaldate')}  # 服务器到达时间")
        print(f"  from         : {m.get('from')}")
        print(f"  to           : {m.get('to')}")
        print(f"  cc           : {m.get('cc')}")
        print(f"  bcc          : {m.get('bcc')}")
        print(f"  reply_to     : {m.get('reply_to')}")
        print(f"  subject      : {m.get('subject')}")
        print(f"  type         : {m.get('type')}")
        print(f"  summary      : {m.get('summary')}")
        if m.get("code"):
            print(f"  code         : {m.get('code')}")
        print(f"  message_id   : {m.get('message_id')}")
        print(f"  in_reply_to  : {m.get('in_reply_to')}")
        print(f"  references   : {m.get('references')}")
        print(f"  content_type : {m.get('content_type')}")
        print(f"  attachments  : {m.get('attachments')}")
        print(f"  body_text_len: {m.get('body_text_len')}")
        print(f"  body_html_len: {m.get('body_html_len')}")
        if m.get("body_text"):
            print("  ---- body_text ----")
            _safe_print(m["body_text"])
        if m.get("body_html") and args.html:
            print("  ---- body_html ----")
            _safe_print(m["body_html"])
    return 0


def _safe_print(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.buffer.write((text + "\n").encode(enc, errors="replace"))


def cmd_mail_send(args: argparse.Namespace) -> int:
    _, account = _pick_account(args.account)
    client = _mail_client(account)
    try:
        client.send(to=args.to, subject=args.subject, body=args.body)
    except Exception as e:
        print(f"[{account.name}] 发送失败: {e}")
        return 1
    print(f"[{account.name}] 已发送 -> {args.to}")
    print(f"  subject={args.subject}")
    return 0


def cmd_mail_get(args: argparse.Namespace) -> int:
    import json

    mail = args.account
    if not mail:
        settings, account = _pick_account(None)
        mail = account.mail
    try:
        data = get_mail_by_uid(
            mail=mail,
            uid=args.uid,
            mailbox=args.box,
            include_body=not args.no_body,
            body_limit=args.body_limit,
            search_all=args.all_boxes,
        )
    except Exception as e:
        print(f"[{mail}] 获取失败: {e}")
        return 1

    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_mail_sync(args: argparse.Namespace) -> int:
    """增量收取邮件并入库（只存元数据+分类，正文不落库）。"""
    from tools.mail_sync import MailSyncService

    settings = load_settings()
    db = get_db()
    service = MailSyncService(db)

    if args.all_accounts:
        _, accounts = load_all_accounts(settings.accounts_files, settings.base_dir)
        if not accounts:
            raise SystemExit("未加载到任何账户，请检查 accounts/ 与 .env")
        targets = accounts
    else:
        _, account = _pick_account(args.account)
        targets = [account]

    boxes = [b.strip() for b in (args.box or "").split(",") if b.strip()] or None
    stats = service.sync_accounts(
        targets,
        mailboxes=boxes,
        limit=args.limit,
        full=args.full,
        on_progress=(lambda msg: _safe_print(f"  .. {msg}")) if args.verbose else None,
    )

    total_saved = sum(s.saved for s in stats)
    total_updated = sum(s.updated for s in stats)
    failed = [s for s in stats if not s.ok]
    for s in stats:
        from tools.mail import mailbox_label as _mailbox_label

        head = f"[{s.account}] {_mailbox_label(s.mailbox)}"
        if not s.ok:
            _safe_print(f"{head} FAIL: {s.error}")
            continue
        by_type = ", ".join(f"{k}={v}" for k, v in sorted(s.by_type.items())) or "-"
        _safe_print(
            f"{head} fetched={s.fetched} new={s.saved} updated={s.updated} "
            f"alias_matched={s.matched_alias} last_uid={s.last_uid} [{by_type}]"
        )

    _safe_print(f"总计: 新增 {total_saved}，更新 {total_updated}，失败 {len(failed)}")
    if settings.debug:
        for s in stats:
            _safe_print(f"  debug: {s.to_dict()}")
    return 1 if failed and total_saved == 0 and total_updated == 0 else 0


def cmd_mail_export_codes(args: argparse.Namespace) -> int:
    """导出验证码邮件，写完将 CSV 设置为只读。"""
    from tools.mail_export import export_verification_codes_csv

    settings = load_settings()
    output = Path(args.output)
    if not output.is_absolute():
        output = settings.base_dir / output
    try:
        path, count = export_verification_codes_csv(get_db(), output)
    except Exception as exc:
        _safe_print(f"验证码 CSV 导出失败: {type(exc).__name__}: {exc}")
        return 1
    _safe_print(f"验证码 CSV 已导出: {path}  rows={count}  readonly=Y")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    """启动 Web 界面（邮箱池子 + 收件展示）。"""
    try:
        import uvicorn
    except ImportError:
        raise SystemExit(
            "缺少 uvicorn，请先安装：pip install -r requirements.txt"
        ) from None

    import os

    settings = apply_cli_domain(load_settings(), args)
    os.environ["ICLOUD_DOMAIN"] = settings.domain
    from web.deps import get_settings

    get_settings.cache_clear()

    # 复用模块级单例，避免重复装配（重复解析账户、重复挂载静态目录）
    from web.app import app

    _safe_print(f"Web 已启动: http://{args.host}:{args.port}  domain={settings.domain}")
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def _log_dir(settings) -> Path:
    raw = (settings.log_dir or "logs").strip()
    p = Path(raw)
    if not p.is_absolute():
        p = settings.base_dir / p
    return p


def cmd_camoufox_fetch(_: argparse.Namespace) -> int:
    settings = load_settings()
    logger, log_path = setup_logger(
        "2608b.camoufox",
        _log_dir(settings),
        file_prefix="camoufox-fetch",
    )
    print(f"log: {log_path}")
    print(f"target: {resolve_camoufox_dir(settings)}")
    try:
        path = fetch_browser(settings, logger=logger)
    except Exception as e:
        logger.exception("fetch failed: %s", e)
        print(f"失败: {e}")
        return 1
    st = browser_status(settings)
    print(f"OK install_dir={path}")
    print(f"  installed={st['installed']} exe={st['executable']}")
    print(f"  under_project={st['under_project']}")
    return 0


def cmd_camoufox_path(_: argparse.Namespace) -> int:
    settings = load_settings()
    st = browser_status(settings)
    print(f"CAMOUFOX_DIR={settings.camoufox_dir}")
    print(f"install_dir={st['install_dir']}")
    print(f"installed={st['installed']}")
    print(f"executable={st['executable'] or '-'}")
    print(f"under_project={st['under_project']}")
    return 0 if st["installed"] else 1


def cmd_cookie_login(args: argparse.Namespace) -> int:
    settings, account, created_path = _pick_or_create_cookie_account(args.account, args)
    logger, log_path = setup_logger(
        "2608b.cookie_login",
        _log_dir(settings),
        file_prefix="cookie-login",
        account=account.name,
    )
    # 让 runtime logger 也打到同一套 handler
    import logging

    runtime_logger = logging.getLogger("2608b.camoufox")
    runtime_logger.handlers.clear()
    runtime_logger.setLevel(logging.DEBUG)
    runtime_logger.propagate = False
    for h in logger.handlers:
        runtime_logger.addHandler(h)

    if created_path:
        print(f"已创建账户配置: {created_path}")
    print(f"account: {account.name}")
    print(f"file: {account.source}")
    print(f"log: {log_path}")
    print(f"region domain: {settings.domain}  origin: {settings.origin}")
    headless = bool(getattr(args, "headless", False))
    if headless:
        print("模式: 无头浏览器；复用 db/cookie/ 中的现有登录会话")
    else:
        print("模式: 有头浏览器；请在窗口内登录，2FA 验证码在浏览器里输入")
    print("等待必填 Cookie 齐全后自动写回 YAML…")
    if getattr(args, "keep_open", 0):
        print(f"采集成功后浏览器再挂 {int(args.keep_open)} 秒再关")
    if getattr(args, "debug", False):
        print("debug: 页面内容变化时落盘 HTML/文本，目录见 logs/page-debug-...")
    reuse = not bool(getattr(args, "no_reuse_session", False))
    if reuse:
        print("session: 尝试从 db/cookie/ 注入上次会话，并在本次写回")
    start_url = args.url
    follow_appleid = bool(getattr(args, "appleid", False))
    if follow_appleid:
        print(f"login: {settings.origin}/  登录成功后再打开 {settings.appleid_origin}/")

    opts = CookieCaptureOptions(
        headless=headless,
        timeout_sec=float(args.timeout),
        url=start_url,
        backup=not args.no_backup,
        keep_open_sec=float(getattr(args, "keep_open", 0) or 0),
        debug=bool(getattr(args, "debug", False)),
        reuse_session=reuse,
        follow_appleid=follow_appleid,
        appleid_url=settings.appleid_origin + "/",
        capture_forward_to=not bool(getattr(args, "no_forward_to", False)),
    )
    try:
        result = capture_icloud_cookie(
            settings, account, opts, logger=logger
        )
    except CamoufoxRuntimeError as e:
        print(f"失败: {e}")
        return 1

    if result.debug_dir:
        print(f"debug dumps: {result.debug_dir}  pages={result.debug_pages}")

    if result.ok:
        get_db().clear_cookie_invalid(account.name)
        print(f"OK stage={result.stage} keys={len(result.keys)} {result.message}")
        print(f"  wrote: {result.account_path}")
        if result.apple_id:
            print(f"  Apple ID: {result.apple_id}")
        if result.apple_id_error:
            print(f"  Apple ID 回填警告: {result.apple_id_error}")
        if result.forward_to:
            print(f"  转发至: {result.forward_to}")
            if result.forward_options:
                print(f"  候选邮箱: {', '.join(result.forward_options)}")
        if result.forward_error:
            print(f"  转发邮箱采集警告: {result.forward_error}")
        print("  cookie_invalid 标记已清除")
        return 0

    print(f"失败 stage={result.stage}: {result.message}")
    if result.missing:
        print(f"  missing: {', '.join(result.missing)}")
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="main.py", description="2608B iCloud Hide My Email CLI")
    sub = p.add_subparsers(dest="command", required=True)

    p_acc = sub.add_parser("accounts", help="列出本地账户与 cookie 完整性")
    add_region_arguments(p_acc)
    p_acc.set_defaults(func=cmd_accounts)

    p_cf = sub.add_parser("camoufox-fetch", help="下载 Camoufox 到项目 browsers/camoufox")
    p_cf.set_defaults(func=cmd_camoufox_fetch)

    p_cp = sub.add_parser("camoufox-path", help="显示项目内 Camoufox 路径与安装状态")
    p_cp.set_defaults(func=cmd_camoufox_path)

    p_cl = sub.add_parser(
        "cookie-login",
        help="登录或复用 iCloud 会话，采集 Cookie 和转发邮箱写回账户 YAML",
    )
    p_cl.add_argument("-a", "--account", help="账户/邮箱；不存在时自动创建账户 YAML")
    p_cl.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="等待登录超时秒数，默认 600",
    )
    p_cl.add_argument("--url", default=None, help="登录起始 URL，默认 https://www.<domain>/")
    p_cl.add_argument(
        "--headless",
        action="store_true",
        help="无头运行；默认复用 db/cookie/ 的既有登录会话",
    )
    p_cl.add_argument(
        "--no-backup",
        action="store_true",
        help="写回前不生成 .bak",
    )
    p_cl.add_argument(
        "--keep-open",
        type=int,
        default=0,
        metavar="SEC",
        help="Cookie 采集成功后浏览器再开 SEC 秒再关，默认 0 立即关",
    )
    p_cl.add_argument(
        "--debug",
        action="store_true",
        help="采集每个新页面的 URL / 标题 / HTML / 可见文本到 logs/page-debug-...",
    )
    p_cl.add_argument(
        "--appleid",
        action="store_true",
        help="先在 iCloud 登录，cookie 齐后再打开 /settings/（Apple 账户信息所在页）",
    )
    p_cl.add_argument(
        "--no-reuse-session",
        action="store_true",
        help="不从 db/cookie/ 注入上次会话（默认会注入并写回）",
    )
    p_cl.add_argument(
        "--no-forward-to",
        action="store_true",
        help="跳过隐藏邮件页面的转发邮箱读取与 inbox.mail 回填",
    )
    add_region_arguments(p_cl)
    p_cl.set_defaults(func=cmd_cookie_login)

    p_list = sub.add_parser("list", help="列出 HME 别名并同步到 db")
    p_list.add_argument("-a", "--account", help="账户备注名")
    add_region_arguments(p_list)
    p_list.set_defaults(func=cmd_list)

    p_db = sub.add_parser("db", help="查看本地 db 中的别名记录")
    p_db.add_argument("-a", "--account", help="按账户过滤")
    p_db.set_defaults(func=cmd_db)

    p_cdk = sub.add_parser("cdk", help="通过 CDK 查询隐私邮箱与母号")
    p_cdk.add_argument("--cdk", help="CDK_xxx（或仅 hex 体）")
    p_cdk.add_argument("--list-map", action="store_true", help="导出全部 CDK 映射表")
    p_cdk.add_argument("-a", "--account", help="--list-map 时按母号过滤")
    p_cdk.set_defaults(func=cmd_cdk)

    p_gen = sub.add_parser("generate", help="生成并保留一个 HME 别名（1小时5个，间隔13-15分钟）")
    p_gen.add_argument("-a", "--account", help="账户备注名")
    p_gen.add_argument(
        "-l",
        "--label",
        help="别名标签；默认 CDK_<sha256_hex>（完整64位hex）",
    )
    p_gen.add_argument(
        "--cdk-hex-len",
        type=int,
        default=64,
        help="默认 cdk 标签的 sha256 hex 长度，8-64，默认 64",
    )
    p_gen.add_argument("-n", "--note", default="由 2608B_iCloud 生成", help="备注")
    add_region_arguments(p_gen)
    p_gen.set_defaults(func=cmd_generate)

    p_quota = sub.add_parser("quota", help="查看 HME 创建配额（1小时5个 + 13-15分钟间隔）")
    p_quota.add_argument("-a", "--account", help="账户/邮箱")
    p_quota.set_defaults(func=cmd_quota)

    p_on = sub.add_parser("on", help="恢复别名转发")
    p_on.add_argument("anonymous_id", help="anonymousId")
    p_on.add_argument("-a", "--account", help="账户备注名")
    add_region_arguments(p_on)
    p_on.set_defaults(func=cmd_toggle, action="on")

    p_off = sub.add_parser("off", help="停用别名转发")
    p_off.add_argument("anonymous_id", help="anonymousId")
    p_off.add_argument("-a", "--account", help="账户备注名")
    add_region_arguments(p_off)
    p_off.set_defaults(func=cmd_toggle, action="off")

    p_probe = sub.add_parser("mail-probe", help="测试 IMAP/SMTP 能否登录")
    p_probe.add_argument("-a", "--account", help="账户/邮箱")
    p_probe.set_defaults(func=cmd_mail_probe)

    p_inbox = sub.add_parser("mail-inbox", help="读取邮件详情（含发送/到达时间、正文等）")
    p_inbox.add_argument("-a", "--account", help="账户/邮箱")
    p_inbox.add_argument("-n", "--limit", type=int, default=5, help="条数，默认 5")
    p_inbox.add_argument("--box", default="INBOX", help="文件夹：INBOX/Junk/...")
    p_inbox.add_argument("--no-body", action="store_true", help="不拉正文")
    p_inbox.add_argument("--html", action="store_true", help="同时打印 HTML 正文")
    p_inbox.add_argument("--body-limit", type=int, default=2000, help="正文截断长度")
    p_inbox.set_defaults(func=cmd_mail_inbox)

    p_send = sub.add_parser("mail-send", help="发送一封测试邮件")
    p_send.add_argument("-a", "--account", help="账户/邮箱")
    p_send.add_argument("--to", required=True, help="收件人")
    p_send.add_argument("--subject", default="2608B iCloud test", help="主题")
    p_send.add_argument("--body", default="hello from 2608B_iCloud", help="正文")
    p_send.set_defaults(func=cmd_mail_send)

    p_get = sub.add_parser("mail-get", help="按 主邮箱+uid 获取单封邮件字典")
    p_get.add_argument("-a", "--account", help="主邮箱名")
    p_get.add_argument("--uid", required=True, help="邮件 UID")
    p_get.add_argument("--box", default="INBOX", help="文件夹，默认 INBOX")
    p_get.add_argument("--all-boxes", action="store_true", help="在所有文件夹中查找 uid")
    p_get.add_argument("--no-body", action="store_true", help="不拉正文")
    p_get.add_argument("--body-limit", type=int, default=0, help="正文截断，0=不截断")
    p_get.set_defaults(func=cmd_mail_get)

    p_sync = sub.add_parser(
        "mail-sync", help="增量收取邮件并入库（元数据+分类，正文不落库）"
    )
    p_sync.add_argument("-a", "--account", help="账户/邮箱")
    p_sync.add_argument("--all-accounts", action="store_true", help="收取全部账户")
    p_sync.add_argument(
        "--box", default="", help="逗号分隔文件夹；默认按 provider 取 INBOX,Junk"
    )
    p_sync.add_argument("-n", "--limit", type=int, default=200, help="每目录上限，默认 200")
    p_sync.add_argument("--full", action="store_true", help="忽略水位，全量重扫")
    p_sync.add_argument("-v", "--verbose", action="store_true", help="打印进度")
    p_sync.set_defaults(func=cmd_mail_sync)

    p_export_codes = sub.add_parser(
        "mail-export-codes", help="导出验证码邮件 CSV（写前解除只读，写后设为只读）"
    )
    p_export_codes.add_argument(
        "-o",
        "--output",
        default="sava/verification_codes.csv",
        help="输出路径，默认 sava/verification_codes.csv",
    )
    p_export_codes.set_defaults(func=cmd_mail_export_codes)

    p_web = sub.add_parser("web", help="启动 Web 界面（邮箱池子 + 收件展示）")
    p_web.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    p_web.add_argument("--port", type=int, default=8770, help="端口，默认 8770")
    p_web.add_argument("--log-level", default="info", help="uvicorn 日志级别")
    add_region_arguments(p_web)
    p_web.set_defaults(func=cmd_web)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
