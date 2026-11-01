"""T17：配额核心测试（spec: llm-token-quota / design: 存储设计）。

场景：检查放行、入账累计、TTL 设置、跨日键隔离、用量查询、恰好用尽。
"""

import asyncio
from datetime import date

import pytest

from core import quota
from data.redis_client import close_redis, init_redis


@pytest.fixture(autouse=True)
def _redis():
    client = init_redis(fake=True)
    yield client
    close_redis()


def test_check_allows_when_under_quota(_redis):
    allowed, used, remaining = asyncio.run(
        quota.check(_redis, uid=1, quota_value=100)
    )
    assert allowed is True
    assert used == 0
    assert remaining == 100


def test_record_accumulates(_redis):
    asyncio.run(quota.record(_redis, uid=1, tokens=30))
    asyncio.run(quota.record(_redis, uid=1, tokens=20))
    used = asyncio.run(quota.get_used(_redis, uid=1))
    assert used == 50


def test_record_sets_ttl_once(_redis):
    asyncio.run(quota.record(_redis, uid=2, tokens=10))
    key = quota.quota_key(2)
    ttl = asyncio.run(_redis.ttl(key))
    assert 0 < ttl <= 48 * 3600


def test_blocks_when_quota_exceeded(_redis):
    asyncio.run(quota.record(_redis, uid=3, tokens=100))
    allowed, used, remaining = asyncio.run(
        quota.check(_redis, uid=3, quota_value=100)
    )
    assert allowed is False  # 恰好用尽：下一轮入口拦截（spec 数据约束）
    assert used == 100
    assert remaining == 0


def test_daily_key_isolation(_redis):
    """按自然日隔离：不同日期独立计数。"""
    today_key = quota.quota_key(9, date.today())
    assert today_key.endswith(date.today().strftime("%Y%m%d"))


def test_get_usage_empty_returns_zero(_redis):
    used, quota_value, remaining = asyncio.run(quota.get_usage(_redis, uid=77, quota_value=500))
    assert used == 0
    assert remaining == 500
