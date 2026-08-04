from __future__ import annotations

import random
import string
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .client import ICloudHMEClient
from .rate_limit import HME_CREATE_LIMIT_PER_HOUR, HMECreateRateLimitError

if TYPE_CHECKING:
    from .db import AliasDB


@dataclass
class HMEAlias:
    hme: str
    label: str
    is_active: bool
    anonymous_id: str
    create_timestamp: int | None = None
    raw: dict | None = None

    @classmethod
    def from_api(cls, item: dict) -> HMEAlias:
        ts = item.get("createTimestamp") or item.get("createdAt")
        try:
            create_timestamp = int(ts) if ts is not None else None
        except (TypeError, ValueError):
            create_timestamp = None
        return cls(
            hme=str(item.get("hme") or ""),
            label=str(item.get("label") or ""),
            is_active=bool(item.get("isActive", True)),
            anonymous_id=str(item.get("anonymousId") or ""),
            create_timestamp=create_timestamp,
            raw=item,
        )


class HMEService:
    """Hide My Email 业务封装。

    硬性规矩：每个账户滚动 1 小时内最多创建 HME_CREATE_LIMIT_PER_HOUR 个隐私邮箱。
    create_alias() 必须传入 account + db，创建前检查、成功后记账。
    """

    def __init__(self, client: ICloudHMEClient, db: AliasDB | None = None) -> None:
        self.client = client
        self.db = db

    def list_aliases(self) -> list[HMEAlias]:
        res = self.client.call_api("/v2/hme/list", "GET")
        items = (((res or {}).get("result") or {}).get("hmeEmails")) or []
        aliases = [HMEAlias.from_api(x) for x in items if isinstance(x, dict)]
        aliases.sort(key=lambda a: a.create_timestamp or 0, reverse=True)
        return aliases

    def generate_raw(self, lang: str = "zh-cn") -> dict[str, Any]:
        """底层 generate。请优先使用 create_alias（含限流）。"""
        return self.client.call_api("/v1/hme/generate", "POST", {"lang": lang})

    def reserve(self, hme: str, label: str, note: str = "由 2608B_iCloud 生成") -> dict[str, Any]:
        """底层 reserve。请优先使用 create_alias（含限流）。"""
        return self.client.call_api(
            "/v1/hme/reserve",
            "POST",
            {"hme": hme, "label": label, "note": note},
        )

    def create_alias(
        self,
        account: str,
        label: str | None = None,
        note: str = "由 2608B_iCloud 生成",
        lang: str = "zh-cn",
        db: AliasDB | None = None,
    ) -> HMEAlias:
        """
        创建并保留一个 HME 别名。

        必须提供 account；db 用于限流检查与 create_events 记账。
        规则：1 小时内最多创建 5 个（每账户）。
        """
        if not account or not str(account).strip():
            raise ValueError("create_alias 必须提供 account（用于限流）")

        store = db or self.db
        if store is None:
            raise RuntimeError(
                "create_alias 必须提供 AliasDB（构造 HMEService(db=...) 或参数 db=...），"
                f"以强制执行每小时最多 {HME_CREATE_LIMIT_PER_HOUR} 个的规矩"
            )

        # 创建前硬检查
        quota = store.assert_can_create(account)
        print(
            f"[rate-limit] {account}: {quota.used}/{quota.limit} used in 1h, "
            f"remaining={quota.remaining}"
        )

        gen = self.generate_raw(lang=lang)
        if not gen.get("success") or not ((gen.get("result") or {}).get("hme")):
            raise RuntimeError(f"分配失败: {gen}")

        hme = gen["result"]["hme"]
        if not label:
            suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
            label = f"Alias_{suffix}"

        res = self.reserve(hme=hme, label=label, note=note)
        if not res.get("success"):
            raise RuntimeError(f"保留邮箱失败: {res}")

        result = res.get("result") or {}
        item: dict[str, Any] = {}
        if isinstance(result, dict):
            nested = result.get("hme")
            if isinstance(nested, dict):
                item = nested
            elif result.get("anonymousId") or isinstance(result.get("hme"), str):
                item = result

        if item:
            alias = HMEAlias.from_api(item)
            if not alias.hme:
                alias.hme = hme
            if not alias.label:
                alias.label = label
            if alias.raw is None:
                alias.raw = item
        else:
            alias = HMEAlias(
                hme=hme,
                label=label,
                is_active=True,
                anonymous_id=str(result.get("anonymousId") or "") if isinstance(result, dict) else "",
                raw=res if isinstance(res, dict) else None,
            )

        # 成功后记账（限流）+ 写入 aliases 表
        created_at = store.record_create_event(account, hme=alias.hme, label=alias.label)
        store.upsert_alias(
            account=account,
            hme=alias.hme,
            label=alias.label,
            anonymous_id=alias.anonymous_id,
            is_active=alias.is_active,
            create_timestamp=alias.create_timestamp,
            note=note,
            source="generate",
            raw=alias.raw,
        )
        print(f"[rate-limit] recorded create_event at {created_at} for {alias.hme}")
        return alias

    def deactivate(self, anonymous_id: str) -> dict[str, Any]:
        return self.client.call_api(
            "/v1/hme/deactivate",
            "POST",
            {"anonymousId": anonymous_id},
        )

    def reactivate(self, anonymous_id: str) -> dict[str, Any]:
        return self.client.call_api(
            "/v1/hme/reactivate",
            "POST",
            {"anonymousId": anonymous_id},
        )

    def set_active(self, anonymous_id: str, active: bool) -> dict[str, Any]:
        return self.reactivate(anonymous_id) if active else self.deactivate(anonymous_id)


__all__ = [
    "HMEAlias",
    "HMEService",
    "HMECreateRateLimitError",
    "HME_CREATE_LIMIT_PER_HOUR",
]
