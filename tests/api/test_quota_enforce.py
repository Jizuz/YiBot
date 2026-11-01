"""T18：配额前置拦截测试（spec: llm-token-quota / design: 降级链#3）。

场景：超限 429 业务码且编排零调用、未超限放行、观察模式（QUOTA_ENFORCE=false）
放行、/user/quota 真实数据、请求期间计量身份生效（graph 内 LLM 结果入账）、
剩余量预警话术。
"""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.outputs import LLMResult
from sqlalchemy import BigInteger, Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.chat import router as chat_router
from api.deps.auth import get_current_user
from api.deps.db import get_db
from api.responses import BIZ_CODE, TraceIdMiddleware, register_exception_handlers
from api.routes.auth import router as auth_router
from core import quota
from core import security
from data.redis_client import close_redis, init_redis
from database.conn import Base
from database.models.user import User
from model.callbacks.token_meter import TokenMeterCallbackHandler


class FakeGraph:
    """模拟编排：调用一次 meter（等价真实链路的全局 callback 触发）。"""

    def __init__(self, redis):
        self.calls = 0
        self.redis = redis

    async def ainvoke(self, state, config):
        self.calls += 1
        meter = TokenMeterCallbackHandler(self.redis)
        meter.on_llm_end(
            LLMResult(generations=[], llm_output={"token_usage": {"total_tokens": 40}}),
            run_manager=None,
        )
        # 让出控制权，使异步入账任务（create_task）完成调度
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return {
            "messages": [AIMessage(content="回复内容")],
            "need_emergency": False,
            "active_agent": None,
        }


@pytest.fixture
def client_fixture(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "s1")
    monkeypatch.setenv("JWT_REFRESH_SECRET", "s2")
    monkeypatch.setenv("LLM_DAILY_TOKEN_QUOTA", "100")
    security.get_settings.cache_clear()

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    User.__table__.c.id.type = BigInteger().with_variant(Integer(), "sqlite")
    Base.metadata.create_all(engine, tables=[User.__table__])
    session_factory = sessionmaker(bind=engine)
    redis = init_redis(fake=True)

    app = FastAPI()
    app.add_middleware(TraceIdMiddleware)
    register_exception_handlers(app)
    app.state.redis = redis
    app.state.graph = FakeGraph(redis)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.include_router(chat_router)
    app.include_router(auth_router)
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client, app, redis

    close_redis()
    security.get_settings.cache_clear()
    engine.dispose()


def _auth_headers():
    token = security.issue_access_only(9, "13800000009", "user")
    return {"Authorization": f"Bearer {token}"}


def _ask(client, **params):
    base = {"question": "头疼怎么办", "session_id": "sid-q"}
    base.update(params)
    return client.get("/chat/agent", params=base, headers=_auth_headers())


# ---------------- 前置拦截 ----------------

def test_over_quota_blocked_without_graph_call(client_fixture):
    client, app, redis = client_fixture
    asyncio.run(quota.record(redis, uid=9, tokens=100))  # 恰好用尽

    resp = _ask(client)
    assert resp.status_code == 429
    body = resp.json()
    assert body["code"] == BIZ_CODE.QUOTA_EXCEEDED
    assert "额度" in body["msg"]
    assert app.state.graph.calls == 0  # 编排零调用（CR 阻断项）


def test_under_quota_passes_and_records(client_fixture):
    client, app, redis = client_fixture
    asyncio.run(quota.record(redis, uid=9, tokens=50))

    resp = _ask(client)
    assert resp.status_code == 200
    assert app.state.graph.calls == 1
    # 请求期间 meter 身份生效：graph 内模拟的 LLM 调用（40 token）已入账
    used = asyncio.run(quota.get_used(redis, uid=9))
    assert used == 90


def test_observe_mode_passes_when_disabled(client_fixture, monkeypatch):
    client, app, redis = client_fixture
    monkeypatch.setenv("QUOTA_ENFORCE", "false")
    security.get_settings.cache_clear()
    asyncio.run(quota.record(redis, uid=9, tokens=100))

    resp = _ask(client)
    assert resp.status_code == 200  # 观察模式：仅计量不拦截
    assert app.state.graph.calls == 1


# ---------------- 用量查询 ----------------

def test_quota_endpoint_returns_real_usage(client_fixture):
    client, _, redis = client_fixture
    asyncio.run(quota.record(redis, uid=9, tokens=30))

    resp = client.get("/user/quota", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["used"] == 30
    assert data["quota"] == 100
    assert data["remaining"] == 70
    assert data["date"]


# ---------------- 预警话术 ----------------

def test_warn_message_when_remaining_low(client_fixture):
    client, _, redis = client_fixture
    asyncio.run(quota.record(redis, uid=9, tokens=80))  # 一次调用 40 后剩余 <20%

    resp = _ask(client)
    assert resp.status_code == 200
    assert "额度" in resp.json()["reply"]
