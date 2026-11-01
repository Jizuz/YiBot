"""LLM 用量配额核心（T17，spec: llm-token-quota / design: 存储设计）。

- 键：quota:llm:{uid}:{yyyymmdd}，INCRBY 累计，TTL 48h（对账窗口）；
- 检查（读）与入账（写）异步解耦，容忍 ≤1 轮超放（spec 数据约束）；
- meter 身份（uid）经 contextvar 传播（请求入口 set，全链路 LLM 调用共享）。
"""

import contextvars
from datetime import date

QUOTA_TTL_SECONDS = 48 * 3600

_meter_uid_var: contextvars.ContextVar[object | None] = contextvars.ContextVar(
    "meter_uid", default=None
)


def set_meter_uid(uid):
    """设置当前上下文的计量身份，返回 Token（用 reset_meter_uid 恢复）。"""
    return _meter_uid_var.set(uid)


def reset_meter_uid(token) -> None:
    _meter_uid_var.reset(token)


def get_meter_uid():
    return _meter_uid_var.get()


def quota_key(uid, day: date | None = None) -> str:
    """按用户按自然日的配额键。"""
    day = day or date.today()
    return f"quota:llm:{uid}:{day.strftime('%Y%m%d')}"


async def check(redis, uid, quota_value: int) -> tuple[bool, int, int]:
    """入口检查：返回 (是否放行, 已用, 剩余)。"""
    used = await get_used(redis, uid)
    remaining = max(quota_value - used, 0)
    return used < quota_value, used, remaining


async def record(redis, uid, tokens: int, day: date | None = None) -> None:
    """入账：INCRBY 原子累计；首次写入设置 TTL（调用方保证异常吞噬语义）。"""
    if tokens <= 0:
        return
    key = quota_key(uid, day)
    new_value = await redis.incrby(key, tokens)
    if new_value == tokens:  # 首次写入：补 TTL
        await redis.expire(key, QUOTA_TTL_SECONDS)


async def get_used(redis, uid, day: date | None = None) -> int:
    value = await redis.get(quota_key(uid, day))
    return int(value) if value else 0


async def get_usage(redis, uid, quota_value: int, day: date | None = None):
    """用量查询（GET /user/quota 数据源）。"""
    used = await get_used(redis, uid, day)
    return used, quota_value, max(quota_value - used, 0)
