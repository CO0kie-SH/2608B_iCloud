from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.accounts import find_account, load_all_accounts
from tools.client import ICloudError, ICloudHMEClient
from tools.config import load_settings
from tools.db import AliasDB
from tools.hme import HMEService
from tools.rate_limit import HME_CREATE_LIMIT_PER_HOUR, HMECreateRateLimitError


def main() -> int:
    parser = argparse.ArgumentParser(description="用类 API 生成一个 HME 隐私别名")
    parser.add_argument(
        "-a",
        "--account",
        help="主邮箱/账户名；仅一个账户时可省略",
    )
    parser.add_argument("-l", "--label", help="别名标签，默认 Alias_XXXX")
    parser.add_argument(
        "-n",
        "--note",
        default="由 generate_alias 生成",
        help="备注",
    )
    args = parser.parse_args()

    settings = load_settings()
    _, accounts = load_all_accounts(settings.accounts_files, settings.base_dir)
    if not accounts:
        print("ERROR: 未加载到任何账户，请检查 accounts/")
        return 1

    if args.account:
        acc = find_account(accounts, args.account)
        if not acc:
            names = ", ".join(a.name for a in accounts)
            print(f"ERROR: 未找到账户 {args.account}（可用: {names}）")
            return 1
    elif len(accounts) == 1:
        acc = accounts[0]
    else:
        names = ", ".join(a.name for a in accounts)
        print(f"ERROR: 多个账户，请指定 -a（可用: {names}）")
        return 1

    if not acc.ok:
        print(f"ERROR: cookie 可能不完整 -> {acc.summary()}")
        return 1

    db = AliasDB(settings.base_dir / "db" / "aliases.db")
    print(f"account={acc.name}")
    q0 = db.get_create_quota(acc.name)
    print(
        f"quota before: {q0.used}/{q0.limit} in 1h, remaining={q0.remaining} "
        f"(rule: max {HME_CREATE_LIMIT_PER_HOUR}/hour)"
    )
    print("generating HME alias via tools.hme.HMEService ...")

    try:
        with ICloudHMEClient(settings, acc.cookies) as client:
            svc = HMEService(client, db=db)
            alias = svc.create_alias(
                account=acc.name,
                label=args.label,
                note=args.note,
            )
    except HMECreateRateLimitError as e:
        print(f"ERROR: 拒绝创建（规矩：1小时最多{HME_CREATE_LIMIT_PER_HOUR}个）: {e}")
        return 2
    except (ICloudError, RuntimeError, ValueError) as e:
        print(f"ERROR: 生成失败: {e}")
        return 1

    q1 = db.get_create_quota(acc.name)
    print("OK")
    print(f"hme={alias.hme}")
    print(f"label={alias.label}")
    print(f"id={alias.anonymous_id}")
    print(f"active={alias.is_active}")
    print(f"quota after: {q1.used}/{q1.limit} remaining={q1.remaining}")
    print(f"db={db.db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
