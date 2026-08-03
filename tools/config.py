from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    app_name: str
    debug: bool
    domain: str
    accounts_files: str
    client_build: str
    client_id: str
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
    )
