from __future__ import annotations

"""HME 生产流水线。"""

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .client import CookieInvalidError, ICloudError, ICloudHMEClient, is_cookie_failure
from .config import settings_for_account
from .db import AliasDB, utc_now
from .hme import HMEService


@dataclass
class ProductionResult:
    requested: int
    threads: int
    created: int
    items: list[dict[str, Any]]
    errors: list[str]


def produce_aliases(
    account: Any,
    *,
    settings: Any,
    db: AliasDB,
    count: int,
    threads: int = 1,
    note: str = "由 2608B_iCloud 生产页生成",
    on_progress: Any = None,
) -> ProductionResult:
    """按单个 iCloud 账号逐个生产，配额由 SQLite 原子占位保证。"""
    count = max(1, min(int(count), 20))
    threads = max(1, min(int(threads), 5, count))
    account_name = getattr(account, "name", "?")
    db.reconcile_cookie_flag(account)
    db.assert_cookie_ready(account_name)
    if not getattr(account, "ok", False):
        db.mark_cookie_invalid(account_name, reason="cookie_incomplete")
        raise CookieInvalidError(account_name, reason="cookie_incomplete")
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    def create_one(index: int) -> dict[str, Any]:
        account_settings = settings_for_account(settings, account)
        with ICloudHMEClient(account_settings, account.cookies) as client:
            service = HMEService(client, db=db)
            if callable(on_progress):
                on_progress(f"{account.name}：开始生产 {index + 1}/{count}")
            try:
                alias = service.create_alias(account=account.name, note=note, db=db)
            except (ICloudError, RuntimeError) as exc:
                if is_cookie_failure(exc):
                    db.mark_cookie_invalid(account_name, reason="http_421")
                    raise CookieInvalidError(account_name, reason="http_421") from exc
                raise
            if callable(on_progress):
                on_progress(f"{account.name}：已生产 {alias.hme} ({index + 1}/{count})")
            return {
                "hme": alias.hme,
                "label": alias.label,
                "anonymous_id": alias.anonymous_id,
                "created_at": utc_now(),
            }

    with ThreadPoolExecutor(max_workers=threads, thread_name_prefix=f"hme-{account.name}") as pool:
        futures = {pool.submit(create_one, index): index for index in range(count)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                items.append(future.result())
            except Exception as exc:
                errors.append(f"第 {index + 1} 个：{type(exc).__name__}: {exc}")
                if callable(on_progress):
                    on_progress(f"{account.name}：失败，{errors[-1]}")
    items.sort(key=lambda item: item["created_at"])
    return ProductionResult(
        requested=count, threads=threads, created=len(items), items=items, errors=errors
    )
