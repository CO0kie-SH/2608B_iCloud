from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def ensure_env_loaded() -> None:
    """
    保证 .env 已加载后再读环境变量。

    模块级常量可能在 load_settings() 之前就被求值，所以凡是延迟读取 env 的地方
    都先调一次这里。load_dotenv 默认不覆盖已存在的环境变量，重复调用是安全的。
    """
    load_dotenv(BASE_DIR / ".env")


def env_str(key: str, default: str) -> str:
    ensure_env_loaded()
    raw = os.getenv(key)
    return raw.strip() if raw and raw.strip() else default


def env_int(key: str, default: int) -> int:
    ensure_env_loaded()
    raw = (os.getenv(key) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_bool(key: str, default: bool) -> bool:
    ensure_env_loaded()
    raw = (os.getenv(key) or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def env_tuple(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """逗号分隔列表；统一小写去空。"""
    ensure_env_loaded()
    raw = (os.getenv(key) or "").strip()
    if not raw:
        return default
    items = tuple(p.strip().lower() for p in raw.split(",") if p.strip())
    return items or default


@dataclass(frozen=True)
class Settings:
    app_name: str
    debug: bool
    domain: str
    accounts_files: str
    client_build: str
    client_id: str
    camoufox_dir: str
    camoufox_proxy: str
    log_dir: str
    base_dir: Path = BASE_DIR

    @property
    def origin(self) -> str:
        return f"https://www.{self.domain}"

    @property
    def setup_host(self) -> str:
        return f"https://setup.{self.domain}"


def load_settings(env_path: Path | None = None) -> Settings:
    path = env_path or (BASE_DIR / ".env")
    load_dotenv(path)

    return Settings(
        app_name=os.getenv("APP_NAME", "2608B_iCloud"),
        debug=os.getenv("DEBUG", "false").lower() == "true",
        domain=os.getenv("ICLOUD_DOMAIN", "icloud.com").strip(),
        accounts_files=os.getenv("ACCOUNTS_FILES")
        or os.getenv("ACCOUNTS_FILE", "accounts/"),
        client_build=os.getenv("CLIENT_BUILD", "2610Hotfix23"),
        client_id=os.getenv("CLIENT_ID", "37bd9669-50c3-4d52-af42-1d240d3ac4f3"),
        camoufox_dir=os.getenv("CAMOUFOX_DIR", "browsers/camoufox").strip(),
        # 空 = 运行时默认 127.0.0.1:7897；none/off 关闭
        camoufox_proxy=os.getenv("CAMOUFOX_PROXY", "").strip(),
        log_dir=os.getenv("LOG_DIR", "logs").strip(),
    )
