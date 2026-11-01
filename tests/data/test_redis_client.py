"""T6：redis.asyncio 客户端单例测试（spec: production-hardening / design: 存储设计）。"""

import asyncio

import pytest

from data.redis_client import close_redis, get_redis, init_redis


@pytest.fixture(autouse=True)
def _reset_singleton():
    yield
    close_redis()


def test_get_redis_before_init_raises():
    with pytest.raises(RuntimeError, match="未初始化"):
        get_redis()


def test_init_fake_and_smoke():
    client = init_redis(fake=True)
    assert get_redis() is client

    async def _smoke():
        await client.set("k1", "v1", ex=60)
        value = await client.get("k1")
        ttl = await client.ttl("k1")
        return value, ttl

    value, ttl = asyncio.run(_smoke())
    assert value == "v1"
    assert 0 < ttl <= 60


def test_init_fake_getdel_atomic():
    client = init_redis(fake=True)

    async def _getdel():
        await client.set("auth:refresh:j1", "42", ex=3600)
        first = await client.getdel("auth:refresh:j1")
        second = await client.getdel("auth:refresh:j1")
        return first, second

    first, second = asyncio.run(_getdel())
    assert first == "42"
    assert second is None  # 二次取出为空 → refresh 轮换幂等依据


def test_close_is_idempotent():
    init_redis(fake=True)
    close_redis()
    close_redis()
    with pytest.raises(RuntimeError):
        get_redis()
