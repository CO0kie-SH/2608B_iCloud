from __future__ import annotations

"""
硬性规矩（必须遵守）：
每个账户在任意滚动 1 小时内，最多创建 5 个隐私邮箱（HME）。
超限必须拒绝，禁止绕过。
"""

# 每账户、滚动 1 小时内最多创建数量
HME_CREATE_LIMIT_PER_HOUR = 5
HME_CREATE_WINDOW_SECONDS = 3600


class HMECreateRateLimitError(RuntimeError):
    """触发 HME 创建频率限制。"""

    def __init__(self, account: str, used: int, limit: int, retry_after_sec: int) -> None:
        self.account = account
        self.used = used
        self.limit = limit
        self.retry_after_sec = max(0, int(retry_after_sec))
        mins = (self.retry_after_sec + 59) // 60
        super().__init__(
            f"RATE_LIMIT: account={account} 近1小时已创建 {used}/{limit} 个隐私邮箱，"
            f"请约 {mins} 分钟后再试（retry_after={self.retry_after_sec}s）"
        )
