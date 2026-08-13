from __future__ import annotations

"""Web 层依赖：配置/账户缓存、账户解析、DB 与邮件客户端构建。"""

from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from tools.accounts import find_account, load_all_accounts, resolve_account_files
from tools.config import Settings, load_settings
from tools.db import AliasDB
from tools.mail import ICloudMailClient, mail_client_from_account
from tools.mail_sync import MailSyncService, default_db_path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


def _accounts_signature(settings: Settings) -> tuple[tuple[str, int], ...]:
    """账户 YAML 的 (路径, mtime) 指纹：改了文件就自动失效，无需重启服务。"""
    # 只列文件不解析 YAML，否则每次请求都全量解析，缓存就白做了
    files = resolve_account_files(settings.accounts_files, settings.base_dir)
    sig: list[tuple[str, int]] = []
    for f in files:
        try:
            sig.append((str(f), f.stat().st_mtime_ns))
        except OSError:
            sig.append((str(f), -1))
    return tuple(sig)


@lru_cache(maxsize=8)
def _load_accounts_cached(_signature: tuple[tuple[str, int], ...]) -> tuple[Any, ...]:
    settings = get_settings()
    _, accounts = load_all_accounts(settings.accounts_files, settings.base_dir)
    return tuple(accounts)


def get_accounts() -> list[Any]:
    settings = get_settings()
    return list(_load_accounts_cached(_accounts_signature(settings)))


def get_db() -> AliasDB:
    return AliasDB(default_db_path(get_settings()))


def get_sync_service() -> MailSyncService:
    return MailSyncService(get_db())


def resolve_account(name: str | None) -> Any:
    """
    复刻 CLI 的 _pick_account 语义，但抛 HTTPException：
    0 个账户 -> 500；指定但找不到 -> 404；只有 1 个 -> 隐式选中；多个未指定 -> 400。
    """
    accounts = get_accounts()
    if not accounts:
        raise HTTPException(
            status_code=500, detail="未加载到任何账户，请检查 accounts/ 与 .env"
        )
    if name:
        acc = find_account(accounts, name)
        if not acc:
            names = ", ".join(a.name for a in accounts)
            raise HTTPException(
                status_code=404, detail=f"未找到账户: {name}（可用: {names}）"
            )
        return acc
    if len(accounts) == 1:
        return accounts[0]
    names = ", ".join(a.name for a in accounts)
    raise HTTPException(
        status_code=400, detail=f"存在多个账户，请指定 account（可用: {names}）"
    )


def get_mail_client(account: Any, *, timeout: float = 30.0) -> ICloudMailClient:
    try:
        return mail_client_from_account(account, timeout=timeout)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"[{getattr(account, 'name', '?')}] 收件凭证不完整: {e}",
        ) from e


def parent_mail_of(account: Any) -> str:
    """统一的母号归一化口径，与 CLI 的 list 同步路径保持一致。"""
    return (getattr(account, "mail", "") or getattr(account, "name", "") or "").strip()


def web_dir() -> Path:
    return Path(__file__).resolve().parent
