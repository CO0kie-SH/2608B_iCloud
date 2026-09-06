from __future__ import annotations

from dataclasses import dataclass
from typing import Any


FREE_STORAGE_BYTES = 5 * 1024**3
PLAN_SOURCES = (
    "includedWithAccountPurchasedPlan",
    "includedWithAppleOnePlan",
    "includedWithSharedPlan",
    "includedWithCompedPlan",
    "includedWithManagedPlan",
)


class ICloudFreePlanError(RuntimeError):
    code = "ICLOUD_FREE_PLAN"

    def __init__(self, account: str) -> None:
        super().__init__(
            f"{self.code}: account={account} 当前套餐为免费 5 GB，已移出生产池。"
            "续费后请运行 main.py plan 复查套餐"
        )


@dataclass(frozen=True)
class ICloudPlan:
    storage_bytes: int
    paid_quota: bool
    free_5gb: bool

    @property
    def name(self) -> str:
        size = f"{self.storage_bytes / 1024**3:g} GB"
        return f"免费 {size}" if self.free_5gb else f"非免费套餐 {size}"

    @classmethod
    def from_responses(cls, storage: Any, plan: Any) -> ICloudPlan:
        # Missing or malformed fields are unknown, never evidence of a free plan.
        if not isinstance(storage, dict) or not isinstance(plan, dict):
            raise ValueError("套餐响应格式异常")
        usage = storage.get("storageUsageInfo")
        quota = storage.get("quotaStatus")
        summary = plan.get("summary")
        if not all(isinstance(item, dict) for item in (usage, quota, summary)):
            raise ValueError("套餐响应缺少容量或来源信息")
        amounts = [usage.get(key) for key in (
            "totalStorageInBytes", "commerceStorageInBytes", "compStorageInBytes"
        )]
        if any(type(value) is not int or value < 0 for value in amounts) or amounts[0] == 0:
            raise ValueError("套餐容量字段异常")
        if type(quota.get("paidQuota")) is not bool:
            raise ValueError("套餐响应缺少付费标记")
        if plan.get("featureKey") != "cloud.storage" or summary.get("includedInPlan") is not True:
            raise ValueError("套餐来源响应未确认存储权益")
        limit = summary.get("limit")
        unit = summary.get("limitUnits")
        if type(limit) not in (int, float) or unit not in ("GIB", "TIB"):
            raise ValueError("套餐来源容量字段异常")
        if limit * (1024**3 if unit == "GIB" else 1024**4) != amounts[0]:
            raise ValueError("套餐容量与来源响应不一致")
        sources = []
        for key in PLAN_SOURCES:
            value = plan.get(key)
            if not isinstance(value, dict) or type(value.get("includedInPlan")) is not bool:
                raise ValueError("套餐来源字段不完整")
            sources.append(value["includedInPlan"])
        free = (
            amounts[0] == FREE_STORAGE_BYTES
            and quota["paidQuota"] is False
            and amounts[1:] == [0, 0]
            and not any(sources)
        )
        return cls(amounts[0], quota["paidQuota"], free)
