"""服务端唯一时钟来源。

所有「当前时间」判定（阶段边界）与所有记录的 created_at 默认值都以此为
唯一依据，便于测试中猴子补丁单个函数即可同时控制两者，保证历史回放类
测试的时间戳确定性。
"""

from datetime import datetime, timezone


def now_utc() -> datetime:
    """服务端当前 UTC 时间，全部截止判定与写入时间戳以此为唯一依据。"""
    return datetime.now(timezone.utc)
