"""T20：SessionManager 异步化测试（spec: production-hardening / design: 选型#7#8）。

覆盖：方法协程化（阻塞客户端出清）、会话创建/复用/关闭、消息上下文追加、
落库经受控后台任务。
"""

import asyncio
import inspect

import pytest

from data import session_manager as sm
from data.redis_client import close_redis, init_redis


@pytest.fixture(autouse=True)
def _redis():
    client = init_redis(fake=True)
    yield client
    close_redis()


@pytest.fixture
def db_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(sm, "submit_db_task", lambda fn, *a, **k: calls.append((fn.__name__, a, k)) or None)
    return calls


def test_methods_are_coroutines():
    for name in (
        "get_or_create_session", "append_chat_message", "close_session",
    ):
        method = getattr(sm.SessionManager, name)
        assert inspect.iscoroutinefunction(method), f"{name} 必须为协程（阻塞出清）"


def test_create_and_reuse_session(db_calls):
    redis = init_redis(fake=True)

    async def _run():
        first = await sm.SessionManager.get_or_create_session("u1")
        second = await sm.SessionManager.get_or_create_session("u1")
        return first, second

    first, second = asyncio.run(_run())
    assert first == second  # 活跃会话复用
    stored = asyncio.run(redis.hgetall(f"agent:session:{first}"))
    assert stored["user_id"] == "u1"
    assert any(c[0] == "create_session" for c in db_calls)  # 落库走受控任务


def test_append_message_grows_context(db_calls):
    async def _run():
        sid = await sm.SessionManager.get_or_create_session("u2")
        await sm.SessionManager.append_chat_message(sid, 0, "头疼")
        await sm.SessionManager.append_chat_message(sid, 1, "多喝水")
        return sid

    sid = asyncio.run(_run())
    redis = init_redis(fake=True)
    context = asyncio.run(redis.lrange(f"agent:context:{sid}", 0, -1))
    assert len(context) == 2
    assert any(c[0] == "add_message" for c in db_calls)


def test_close_session_clears_cache(db_calls):
    async def _run():
        sid = await sm.SessionManager.get_or_create_session("u3")
        await sm.SessionManager.close_session(sid, "u3")
        return sid

    sid = asyncio.run(_run())
    redis = init_redis(fake=True)

    async def _check():
        return (
            await redis.exists(f"agent:session:{sid}"),
            await redis.exists(f"agent:context:{sid}"),
            await redis.exists("agent:user:u3:session"),
        )

    assert asyncio.run(_check()) == (0, 0, 0)
    assert any(c[0] == "update_session_status" for c in db_calls)
