from __future__ import annotations

"""HME 生产流水线。"""

from dataclasses import dataclass, field
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
    network: list[dict[str, Any]] = field(default_factory=list)


def summarize_network(reports: list[dict[str, Any]] | None) -> dict[str, Any]:
    """合并单个生产任务的链路报告，便于 API/前端直接展示。"""
    items = [item for item in (reports or []) if isinstance(item, dict)]
    routes = {str(item.get("route") or "") for item in items}
    if "direct_then_proxy" in routes or {"direct", "proxy"}.issubset(routes):
        route = "direct_then_proxy"
    elif "proxy" in routes:
        route = "proxy"
    elif "direct" in routes:
        route = "direct"
    else:
        route = "not_started"
    success_values = [
        item.get("success")
        if item.get("success") is not None
        else True
        if str(item.get("status") or "").lower() == "success"
        else False
        if str(item.get("status") or "").lower() == "failed"
        else None
        for item in items
    ]
    success = None
    if success_values and all(value is True for value in success_values):
        success = True
    elif any(value is False for value in success_values):
        success = False
    return {
        "route": route,
        "used_proxy": "proxy" in routes or "direct_then_proxy" in routes,
        "success": success,
        "status": "success" if success is True else "failed" if success is False else "unknown",
        "reports": len(items),
        "successful_attempts": sum(int(item.get("successful_attempts") or 0) for item in items),
        "failed_attempts": sum(int(item.get("failed_attempts") or 0) for item in items),
    }


def _network_report(client: Any, *, success: bool) -> dict[str, Any]:
    reporter = getattr(client, "network_report", None)
    if callable(reporter):
        try:
            report = reporter(success=success)
        except TypeError:
            report = reporter()
        if isinstance(report, dict):
            return report
    # 兼容外部注入的旧客户端实现；没有链路观测能力时明确标记未开始。
    return {
        "route": "not_started",
        "used_proxy": False,
        "direct_attempted": False,
        "proxy_attempted": False,
        "successful_attempts": 0,
        "failed_attempts": 0,
        "success": success,
        "status": "success" if success else "failed",
    }


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
    db.assert_production_ready(account_name)
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
            except Exception as exc:
                network = _network_report(client, success=False)
                if is_cookie_failure(exc):
                    db.mark_cookie_invalid(account_name, reason="http_421")
                    wrapped = CookieInvalidError(account_name, reason="http_421")
                    setattr(wrapped, "network_report", network)
                    raise wrapped from exc
                setattr(exc, "network_report", network)
                raise
            if callable(on_progress):
                on_progress(f"{account.name}：已生产 {alias.hme} ({index + 1}/{count})")
            item = {
                "hme": alias.hme,
                "label": alias.label,
                "anonymous_id": alias.anonymous_id,
                "created_at": utc_now(),
            }
            item["_network_report"] = _network_report(client, success=True)
            return item

    network_reports: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=threads, thread_name_prefix=f"hme-{account.name}") as pool:
        futures = {pool.submit(create_one, index): index for index in range(count)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                item = future.result()
                report = item.pop("_network_report", None)
                if isinstance(report, dict):
                    network_reports.append(report)
                items.append(item)
            except Exception as exc:
                report = getattr(exc, "network_report", None)
                if isinstance(report, dict):
                    network_reports.append(report)
                errors.append(f"第 {index + 1} 个：{type(exc).__name__}: {exc}")
                if callable(on_progress):
                    on_progress(f"{account.name}：失败，{errors[-1]}")
    items.sort(key=lambda item: item["created_at"])
    return ProductionResult(
        requested=count,
        threads=threads,
        created=len(items),
        items=items,
        errors=errors,
        network=network_reports,
    )
