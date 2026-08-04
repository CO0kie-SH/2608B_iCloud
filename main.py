from __future__ import annotations

import argparse
import sys

from tools.accounts import find_account, load_all_accounts
from tools.client import ICloudError, ICloudHMEClient
from tools.config import load_settings
from tools.db import AliasDB
from tools.hme import HMEService
from tools.mail import ICloudMailClient, get_mail_by_uid
from tools.rate_limit import HME_CREATE_LIMIT_PER_HOUR, HMECreateRateLimitError


def get_db() -> AliasDB:
    settings = load_settings()
    return AliasDB(settings.base_dir / "db" / "aliases.db")


def cmd_accounts(_: argparse.Namespace) -> int:
    settings = load_settings()
    files, accounts = load_all_accounts(settings.accounts_files, settings.base_dir)

    print(f"{settings.app_name} (domain={settings.domain})")
    print(f"source files ({len(files)}):")
    for f in files:
        flag = "" if f.exists() else " (missing)"
        print(f"  - {f}{flag}")
    print(f"loaded {len(accounts)} account(s)")
    for a in accounts:
        print(f"  - {a.summary()}")
        if a.format_errors:
            print(f"    format: {'; '.join(a.format_errors)}")
        print(f"    mail={a.mail or '-'}  app_pwd={'set' if a.app_password else 'MISSING'}  mail_ready={a.mail_ready}")
        if settings.debug and a.cookies:
            preview = a.cookies[:60] + ("..." if len(a.cookies) > 60 else "")
            print(f"    cookie: {preview}")
    return 0


def _pick_account(name: str | None):
    settings = load_settings()
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


def cmd_list(args: argparse.Namespace) -> int:
    settings, account = _pick_account(args.account)
    if not account.ok:
        print(f"警告: cookie 可能不完整 -> {account.summary()}")

    try:
        with ICloudHMEClient(settings, account.cookies) as client:
            svc = HMEService(client)
            aliases = svc.list_aliases()
    except (ICloudError, RuntimeError) as e:
        print(f"[{account.name}] 失败: {e}")
        return 1

    db = get_db()
    for a in aliases:
        db.upsert_alias(
            account=account.name,
            hme=a.hme,
            label=a.label,
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
            f"  #{r.id} [{state}] {r.hme}  account={r.account}  "
            f"label={r.label}  id={r.anonymous_id}  at={r.created_at}"
        )
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    settings, account = _pick_account(args.account)
    db = get_db()
    try:
        with ICloudHMEClient(settings, account.cookies) as client:
            svc = HMEService(client, db=db)
            alias = svc.create_alias(
                account=account.name,
                label=args.label,
                note=args.note,
            )
    except HMECreateRateLimitError as e:
        print(f"[{account.name}] 拒绝创建（规矩：1小时最多{HME_CREATE_LIMIT_PER_HOUR}个）: {e}")
        return 2
    except (ICloudError, RuntimeError, ValueError) as e:
        print(f"[{account.name}] 生成失败: {e}")
        return 1

    print(f"[{account.name}] 生成成功: {alias.hme}")
    print(f"  label={alias.label}")
    if alias.anonymous_id:
        print(f"  id={alias.anonymous_id}")
    q = db.get_create_quota(account.name)
    print(f"  quota: {q.used}/{q.limit} in 1h, remaining={q.remaining}")
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

    print(f"规矩: 每账户滚动1小时最多创建 {HME_CREATE_LIMIT_PER_HOUR} 个隐私邮箱")
    for acc in targets:
        q = db.get_create_quota(acc.name)
        print(f"[{acc.name}] used={q.used}/{q.limit} remaining={q.remaining} retry_after={q.retry_after_sec}s")
        for ev in q.recent:
            print(f"  - {ev['created_at']}  {ev['hme']}  label={ev['label']}")
    return 0


def cmd_toggle(args: argparse.Namespace) -> int:
    settings, account = _pick_account(args.account)
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


def _mail_client(account) -> ICloudMailClient:
    if not account.mail_ready:
        raise SystemExit(f"[{account.name}] 邮件凭证不完整，请检查 accounts 文件 MAIL|APPPWD|COOKIE")
    return ICloudMailClient(account.mail, account.app_password)


def cmd_mail_probe(args: argparse.Namespace) -> int:
    _, account = _pick_account(args.account)
    client = _mail_client(account)
    print(f"[{account.name}] mail={account.mail}")
    imap = client.probe_imap()
    smtp = client.probe_smtp()
    print(f"  IMAP: {'OK' if imap.ok else 'FAIL'} - {imap.detail}")
    print(f"  SMTP: {'OK' if smtp.ok else 'FAIL'} - {smtp.detail}")
    return 0 if imap.ok and smtp.ok else 1


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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="main.py", description="2608B iCloud Hide My Email CLI")
    sub = p.add_subparsers(dest="command", required=True)

    p_acc = sub.add_parser("accounts", help="列出本地账户与 cookie 完整性")
    p_acc.set_defaults(func=cmd_accounts)

    p_list = sub.add_parser("list", help="列出 HME 别名并同步到 db")
    p_list.add_argument("-a", "--account", help="账户备注名")
    p_list.set_defaults(func=cmd_list)

    p_db = sub.add_parser("db", help="查看本地 db 中的别名记录")
    p_db.add_argument("-a", "--account", help="按账户过滤")
    p_db.set_defaults(func=cmd_db)

    p_gen = sub.add_parser("generate", help="生成并保留一个 HME 别名（受1小时5个限制）")
    p_gen.add_argument("-a", "--account", help="账户备注名")
    p_gen.add_argument("-l", "--label", help="别名标签，默认 Alias_XXXX")
    p_gen.add_argument("-n", "--note", default="由 2608B_iCloud 生成", help="备注")
    p_gen.set_defaults(func=cmd_generate)

    p_quota = sub.add_parser("quota", help="查看 HME 创建配额（1小时5个）")
    p_quota.add_argument("-a", "--account", help="账户/邮箱")
    p_quota.set_defaults(func=cmd_quota)

    p_on = sub.add_parser("on", help="恢复别名转发")
    p_on.add_argument("anonymous_id", help="anonymousId")
    p_on.add_argument("-a", "--account", help="账户备注名")
    p_on.set_defaults(func=cmd_toggle, action="on")

    p_off = sub.add_parser("off", help="停用别名转发")
    p_off.add_argument("anonymous_id", help="anonymousId")
    p_off.add_argument("-a", "--account", help="账户备注名")
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

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
