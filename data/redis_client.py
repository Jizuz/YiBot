"""redis.asyncio 客户端单例（T6，design: 存储设计）。

- lifespan 内 init_redis() 构造、close_redis() 关闭（幂等）；
- 全部业务键必须带 TTL（CR 红线），本模块不限制但使用方遵守；
- 测试以 init_redis(fake=True) 注入 fakeredis，避免真实网络依赖。
"""

import logging

import redis.asyncio as aioredis

from config.settings import get_settings

logger = logging.getLogger("yibot.data.redis")

_client: aioredis.Redis | None = None


def init_redis(fake: bool = False, max_connections: int = 20) -> aioredis.Redis:
    """初始化全局 redis.asyncio 客户端（重复调用返回既有实例）。"""
    global _client
    if _client is not None:
        return _client
    if fake:
        import fakeredis.aioredis

        _client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    else:
        settings = get_settings()
        _client = aioredis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            password=settings.REDIS_PASSWORD or None,
            decode_responses=True,
            max_connections=max_connections,
        )
    logger.info(
        "redis client initialized",
        extra={"event": "redis_init", "fake": fake},
    )
    return _client


def get_redis() -> aioredis.Redis:
    """获取全局客户端；未初始化时快速失败（装配错误应尽早暴露）。"""
    if _client is None:
        raise RuntimeError("redis 客户端未初始化：请在应用 lifespan 中调用 init_redis()")
    return _client


def close_redis() -> None:
    """关闭并清空全局客户端（幂等，供 lifespan shutdown 与测试复位）。"""
    global _client
    if _client is None:
        return
    try:
        # fakeredis 与真客户端关闭接口一致
        try:
            import asyncio

            asyncio.get_running_loop().create_task(_client.aclose())
        except RuntimeError:
            import asyncio

            asyncio.run(_client.aclose())
    except Exception as exc:  # noqa: BLE001 - 关闭失败不阻断停机流程
        logger.warning("redis close failed: %s", exc, extra={"event": "redis_close"})
    finally:
        _client = None
