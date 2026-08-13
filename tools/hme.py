from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .client import ICloudHMEClient
from .rate_limit import HME_CREATE_LIMIT_PER_HOUR, HMECreateRateLimitError
from .secure_random import random_backend_info, secure_random_bytes

if TYPE_CHECKING:
    from .db import AliasDB

# label: CDK_<sha256_hex>
# - 随机源：按系统切换（Linux getrandom//dev/urandom，Windows/macOS secrets）
# - sha256: 固定长度、不可预测
# - 默认 64 位完整 hex；可截断到 16/32（仍建议 >=16）
CDK_PREFIX = "CDK_"
CDK_HEX_LEN_DEFAULT = 64
CDK_HEX_LEN_MIN = 8
CDK_HEX_LEN_MAX = 64
CDK_LABEL_MAX = 256  # Apple 实测至少支持 256


def generate_cdk_label(hex_len: int = CDK_HEX_LEN_DEFAULT) -> str:
    """
    生成隐私邮箱标签：CDK_<sha256_hex>

    熵来源：secure_random_bytes(32)（按 OS 切换安全随机接口）
    编码：SHA256 十六进制，默认完整 64 字符
    示例：CDK_a3f1...（总长 4+64=68）
    """
    n = int(hex_len)
    if n < CDK_HEX_LEN_MIN or n > CDK_HEX_LEN_MAX:
        raise ValueError(f"hex_len must be in [{CDK_HEX_LEN_MIN}, {CDK_HEX_LEN_MAX}]")

    digest = hashlib.sha256(secure_random_bytes(32)).hexdigest()
    body = digest[:n]
    label = f"{CDK_PREFIX}{body}"
    if len(label) > CDK_LABEL_MAX:
        raise ValueError(f"label too long: {len(label)} > {CDK_LABEL_MAX}")
    return label


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
        cdk_hex_len: int = CDK_HEX_LEN_DEFAULT,
    ) -> HMEAlias:
        """
        创建并保留一个 HME 别名。

        必须提供 account；db 用于限流检查与 create_events 记账。
        规则：1 小时内最多创建 5 个（每账户）。
        默认 label：CDK_<sha256_hex>（按系统安全随机 + SHA256）。
        """
        if not account or not str(account).strip():
            raise ValueError("create_alias 必须提供 account（用于限流）")

        store = db or self.db
        if store is None:
            raise RuntimeError(
                "create_alias 必须提供 AliasDB（构造 HMEService(db=...) 或参数 db=...），"
                f"以强制执行每小时最多 {HME_CREATE_LIMIT_PER_HOUR} 个的规矩"
            )

        # 创建前原子占位；并发 Web 客户端共享同一 SQLite 配额。
        claim_id = store.claim_create_slot(account)
        quota = store.get_create_quota(account)
        print(
            f"[rate-limit] {account}: {quota.used}/{quota.limit} used in 1h, "
            f"remaining={quota.remaining}"
        )

        event_recorded = False
        try:
            gen: dict[str, Any] = {}
            for attempt in range(3):
                gen = self.generate_raw(lang=lang)
                if gen.get("success") and ((gen.get("result") or {}).get("hme")):
                    break
                error = gen.get("error") or {}
                transient = str(error.get("errorCode") or "") == "-41017"
                if not transient or attempt >= 2:
                    break
                retry_after = max(1, min(int(error.get("retryAfter") or 2), 10))
                time.sleep(retry_after)
            if not gen.get("success") or not ((gen.get("result") or {}).get("hme")):
                raise RuntimeError(f"分配失败: {gen}")

            hme = gen["result"]["hme"]
            if not label:
                label = generate_cdk_label(hex_len=cdk_hex_len)
            elif len(label) > CDK_LABEL_MAX:
                raise ValueError(f"label 长度 {len(label)} 超过上限 {CDK_LABEL_MAX}")

            res = self.reserve(hme=hme, label=label, note=note)
            if not res.get("success"):
                raise RuntimeError(f"保留邮箱失败: {res}")
        except Exception:
            store.release_create_claim(claim_id)
            raise

        try:
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

            # 成功后记账（限流）+ 写入 aliases（CDK -> 隐私邮箱 + 母号）
            cdk = alias.label if str(alias.label).startswith("CDK_") else label
            created_at = store.record_create_event(
                account,
                hme=alias.hme,
                label=alias.label,
                cdk=cdk,
                parent_mail=account,
            )
            event_recorded = True
            store.upsert_alias(
                account=account,
                parent_mail=account,
                hme=alias.hme,
                label=alias.label,
                cdk=cdk,
                anonymous_id=alias.anonymous_id,
                is_active=alias.is_active,
                create_timestamp=alias.create_timestamp,
                note=note,
                source="generate",
                raw=alias.raw,
            )
        finally:
            # reserve 已成功但本地记账失败时保留 claim 一小时，避免上游已创建
            # 而本地配额回退，导致后续请求越过真实限制。
            if event_recorded:
                store.release_create_claim(claim_id)
        print(f"[rate-limit] recorded create_event at {created_at} for {alias.hme}")
        print(f"[cdk-map] {cdk} -> hme={alias.hme} parent={account}")
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
    "generate_cdk_label",
    "CDK_PREFIX",
    "CDK_HEX_LEN_DEFAULT",
    "random_backend_info",
]
