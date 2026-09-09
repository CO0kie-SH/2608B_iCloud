from __future__ import annotations

import os
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
PROXY_CONFIG_PATH = BASE_DIR / "proxy.yaml"


def load_proxy_config(path: Path | None = None) -> dict[str, Any]:
    """读取根目录代理/项目 YAML，并归一化为环境变量名。"""
    config_path = path or PROXY_CONFIG_PATH
    if not config_path.exists():
        return {}
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"代理配置读取失败: {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"代理配置必须是 YAML 对象: {config_path}")

    aliases = {
        "app_name": "APP_NAME",
        "debug": "DEBUG",
        "domain": "ICLOUD_DOMAIN",
        "icloud_domain": "ICLOUD_DOMAIN",
        "accounts": "ACCOUNTS_FILES",
        "accounts_file": "ACCOUNTS_FILES",
        "accounts_files": "ACCOUNTS_FILES",
        "camoufox_dir": "CAMOUFOX_DIR",
        "camoufox_proxy": "CAMOUFOX_PROXY",
        "client_build": "CLIENT_BUILD",
        "client_id": "CLIENT_ID",
        "log_dir": "LOG_DIR",
        "hme": "HME_PROXY",
        "hme_proxy": "HME_PROXY",
        "mailcom": "MAILCOM_PROXY",
        "mailcom_proxy": "MAILCOM_PROXY",
        "http": "HTTP_PROXY",
        "http_proxy": "HTTP_PROXY",
        "https": "HTTPS_PROXY",
        "https_proxy": "HTTPS_PROXY",
        "all": "ALL_PROXY",
        "all_proxy": "ALL_PROXY",
        "no_proxy": "NO_PROXY",
    }
    result: dict[str, Any] = {}
    sections = {
        "project": raw.get("project"),
        "proxy": raw.get("proxy"),
    }
    for key, value in raw.items():
        if key not in sections:
            sections["project"] = {**(sections.get("project") or {}), key: value}
    for section in sections.values():
        if not isinstance(section, dict):
            continue
        for key, value in section.items():
            normalized = str(key).strip().lower().replace("-", "_")
            env_key = aliases.get(normalized, str(key).strip().upper())
            result[env_key] = value
    return result


def config_value(key: str, default: Any = "", *, proxy_config: dict[str, Any] | None = None) -> Any:
    """读取配置：proxy.yaml 非空值优先，其次是环境变量和默认值。"""
    ensure_env_loaded()
    config = proxy_config if proxy_config is not None else load_proxy_config()
    if key in config:
        value = config[key]
        if value is not None and (not isinstance(value, str) or value.strip()):
            return value
    return os.getenv(key, default)


@lru_cache(maxsize=1)
def ensure_env_loaded() -> None:
    """
    保证 .env 已加载后再读环境变量。

    模块级常量可能在 load_settings() 之前就被求值，所以凡是延迟读取 env 的地方
    都先调一次这里。load_dotenv 默认不覆盖已存在的环境变量，重复调用是安全的。
    """
    load_dotenv(BASE_DIR / ".env")


def env_str(key: str, default: str) -> str:
    raw = config_value(key, default)
    if raw is None:
        return default
    text = str(raw).strip()
    return text if text else default


def env_int(key: str, default: int) -> int:
    raw = str(config_value(key, default) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_bool(key: str, default: bool) -> bool:
    raw = str(config_value(key, str(default)) or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def env_tuple(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """逗号分隔列表；统一小写去空。"""
    configured = config_value(key, "")
    if isinstance(configured, (list, tuple, set)):
        items = tuple(str(p).strip().lower() for p in configured if str(p).strip())
        return items or default
    raw = str(configured or "").strip()
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
    hme_proxy: str = ""
    # 领取凭证里的取码 URL 根地址；空则默认 http://127.0.0.1:8770
    public_base_url: str = ""

    @property
    def origin(self) -> str:
        return f"https://www.{self.domain}"

    @property
    def setup_host(self) -> str:
        return f"https://setup.{self.domain}"

    @property
    def is_cn(self) -> bool:
        return self.domain.endswith(".cn") or self.domain == "icloud.com.cn"

    @property
    def appleid_origin(self) -> str:
        # Apple 账户邮箱出现在 iCloud 设置页，不是 appleid.apple.com.cn（这个域名不存在）
        return f"{self.origin}/settings"


ICLOUD_REGION_PRESETS: dict[str, str] = {
    "cn": "icloud.com.cn",
    "china": "icloud.com.cn",
    "com.cn": "icloud.com.cn",
    "us": "icloud.com",
    "global": "icloud.com",
    "intl": "icloud.com",
    "com": "icloud.com",
}


def resolve_icloud_domain(
    *,
    region: str | None = None,
    suffix: str | None = None,
    domain: str | None = None,
    default: str = "icloud.com",
) -> str:
    """区域 / 后缀 → iCloud Web 域名。cn 预选为 icloud.com.cn。"""
    if domain and str(domain).strip():
        raw = str(domain).strip().lower().lstrip(".")
        if raw in ICLOUD_REGION_PRESETS:
            return ICLOUD_REGION_PRESETS[raw]
        return raw if raw.startswith("icloud.") else f"icloud.{raw}"

    if suffix and str(suffix).strip():
        tail = str(suffix).strip().lower().lstrip(".")
        if tail in {"cn", "com.cn"}:
            return "icloud.com.cn"
        if tail == "com":
            return "icloud.com"
        return f"icloud.{tail}"

    if region and str(region).strip():
        key = str(region).strip().lower().lstrip(".")
        if key in ICLOUD_REGION_PRESETS:
            return ICLOUD_REGION_PRESETS[key]
        if key.startswith("icloud."):
            return key
        if key in {"cn", "com.cn"}:
            return "icloud.com.cn"
        return f"icloud.{key}"

    return default


def settings_with_domain(settings: Settings, domain: str) -> Settings:
    target = (domain or "").strip().lower()
    if not target or target == settings.domain:
        return settings
    return replace(settings, domain=target)


HME_API_DOMAIN = "icloud.com"


def settings_for_account(settings: Settings, account: object) -> Settings:
    """浏览器登录可用账户级 .cn；HME API 统一打国际站 setup.icloud.com。"""
    if not isinstance(settings, Settings):
        return settings
    return settings_with_domain(settings, HME_API_DOMAIN)


def settings_for_browser(settings: Settings, account: object) -> Settings:
    """Camoufox / cookie-login 仍按账户 YAML 的区域打开页面。"""
    domain = str(getattr(account, "icloud_domain", "") or "").strip()
    return settings_with_domain(settings, domain) if domain else settings



def apply_cli_domain(settings: Settings, args: object | None) -> Settings:
    if args is None:
        return settings
    override = resolve_icloud_domain(
        region=getattr(args, "region", None),
        suffix=getattr(args, "suffix", None),
        domain=getattr(args, "domain", None),
        default="",
    )
    return settings_with_domain(settings, override) if override else settings


def add_region_arguments(parser) -> None:
    parser.add_argument(
        "--region",
        default=None,
        metavar="CODE",
        help="iCloud 区域：cn → icloud.com.cn；us/global/com → icloud.com",
    )
    parser.add_argument(
        "--suffix",
        default=None,
        metavar="SFX",
        help="网址后缀，如 com / com.cn / cn（cn 与 com.cn 都是 icloud.com.cn）",
    )


def load_settings(
    env_path: Path | None = None, proxy_path: Path | None = None
) -> Settings:
    path = env_path or (BASE_DIR / ".env")
    load_dotenv(path)
    proxy_config = load_proxy_config(proxy_path)

    def value(key: str, default: Any = "") -> Any:
        return config_value(key, default, proxy_config=proxy_config)

    return Settings(
        app_name=str(value("APP_NAME", "2608B_iCloud")),
        debug=str(value("DEBUG", "false")).lower() == "true",
        domain=str(value("ICLOUD_DOMAIN", "icloud.com")).strip(),
        accounts_files=str(value("ACCOUNTS_FILES", value("ACCOUNTS_FILE", "accounts/"))),
        client_build=str(value("CLIENT_BUILD", "2610Hotfix23")),
        client_id=str(value("CLIENT_ID", "37bd9669-50c3-4d52-af42-1d240d3ac4f3")),
        camoufox_dir=str(value("CAMOUFOX_DIR", "browsers/camoufox")).strip(),
        # 空 = 运行时默认 127.0.0.1:7897；none/off 关闭
        camoufox_proxy=str(value("CAMOUFOX_PROXY", "")).strip(),
        log_dir=str(value("LOG_DIR", "logs")).strip(),
        hme_proxy=str(value("HME_PROXY", "")).strip(),
        public_base_url=str(value("PUBLIC_BASE_URL", "")).strip(),
    )
