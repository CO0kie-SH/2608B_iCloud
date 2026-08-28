from __future__ import annotations

"""
硬性规矩（必须遵守）：
每个账户在任意滚动 1 小时内，最多创建 5 个隐私邮箱（HME）。
超限必须拒绝，禁止绕过。
"""

import secrets

# 每账户、滚动 1 小时内最多创建数量
HME_CREATE_LIMIT_PER_HOUR = 5
HME_CREATE_WINDOW_SECONDS = 3600

# 每个账户最多保有的 HME 总数（停用地址也计入）
HME_ACCOUNT_ALIAS_LIMIT = 740

# 老接口额外间隔：3600/5+1 = 13 分钟起，随机落到 [13, 15] 分钟
HME_CREATE_MIN_INTERVAL_MINUTES = 13
HME_CREATE_MAX_INTERVAL_MINUTES = 15
HME_CREATE_MIN_INTERVAL_SECONDS = HME_CREATE_MIN_INTERVAL_MINUTES * 60
HME_CREATE_MAX_INTERVAL_SECONDS = HME_CREATE_MAX_INTERVAL_MINUTES * 60


def pick_create_interval_seconds() -> int:
    """安全随机挑一个 [13min, 15min] 的间隔（含两端）。"""
    span = HME_CREATE_MAX_INTERVAL_SECONDS - HME_CREATE_MIN_INTERVAL_SECONDS + 1
    return HME_CREATE_MIN_INTERVAL_SECONDS + secrets.randbelow(span)


class HMECreateRateLimitError(RuntimeError):
    """触发 HME 创建频率限制。"""

    def __init__(
        self,
        account: str,
        used: int,
        limit: int,
        retry_after_sec: int,
        reason: str = "hourly",
        next_produce_at: int = 0,
    ) -> None:
        self.account = account
        self.used = used
        self.limit = limit
        self.retry_after_sec = max(0, int(retry_after_sec))
        self.reason = reason or "hourly"
        self.next_produce_at = int(next_produce_at or 0)
        mins = (self.retry_after_sec + 59) // 60
        if self.reason == "interval":
            super().__init__(
                f"RATE_LIMIT: account={account} 距上次生产不足 "
                f"{HME_CREATE_MIN_INTERVAL_MINUTES} 分钟，"
                f"请约 {mins} 分钟后再试（retry_after={self.retry_after_sec}s"
                f" next_produce_at={self.next_produce_at}）"
            )
        else:
            super().__init__(
                f"RATE_LIMIT: account={account} 近1小时已创建 {used}/{limit} 个隐私邮箱，"
                f"请约 {mins} 分钟后再试（retry_after={self.retry_after_sec}s）"
            )


class HMEAccountAliasLimitError(RuntimeError):
    """单账号 HME 总量已达到永久容量上限。"""

    code = "HME_ACCOUNT_LIMIT"

    def __init__(
        self,
        account: str,
        alias_count: int,
        limit: int = HME_ACCOUNT_ALIAS_LIMIT,
        pending: int = 0,
    ) -> None:
        self.account = account
        self.alias_count = max(0, int(alias_count or 0))
        self.limit = max(1, int(limit or HME_ACCOUNT_ALIAS_LIMIT))
        self.pending = max(0, int(pending or 0))
        pending_text = f"，另有 {self.pending} 个正在生产" if self.pending else ""
        super().__init__(
            f"{self.code}: account={account} 已有 {self.alias_count}/{self.limit} 个隐私邮箱"
            f"{pending_text}，已达到单账号生产上限"
        )
