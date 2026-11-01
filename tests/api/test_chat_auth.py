"""T10：业务路由接入鉴权测试（spec: access-auth）。

场景：无令牌 401、令牌身份优先（query 伪造无效）、兼容通道放行+告警、
AUTH_ENABLED=false 止血放行、垃圾令牌拒绝、thread_id/uid 归一化格式不变、
会话历史数据隔离。
"""

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy import BigInteger, Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.chat import router as chat_router
from api.deps.db import get_db
from api.deps.auth import get_current_user
from api.responses import BIZ_CODE, TraceIdMiddleware, register_exception_handlers
from core import security
from data.mysql.conn import Base
from data.mysql.models.session import Session
from data.mysql.models.user import User


class FakeGraph:
    def __init__(self):
        self.calls = []

    async def ainvoke(self, state, config):
        self.calls.append({"state": state, "config": config})
        return {
            "messages": [AIMessage(content="这是回复")],
            "need_emergency": False,
            "active_agent": None,
        }


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-access-secret")
    monkeypatch.setenv("JWT_REFRESH_SECRET", "test-refresh-secret")
    security.get_settings.cache_clear()
    yield monkeypatch
    security.get_settings.cache_clear()


@pytest.fixture
def client_fixture(env):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session.__table__.c.id.type = BigInteger().with_variant(Integer(), "sqlite")
    User.__table__.c.id.type = BigInteger().with_variant(Integer(), "sqlite")
    Base.metadata.create_all(engine, tables=[Session.__table__, User.__table__])
    session_factory = sessionmaker(bind=engine)

    from data.redis_client import close_redis, init_redis

    redis = init_redis(fake=True)

    app = FastAPI()
    app.add_middleware(TraceIdMiddleware)
    register_exception_handlers(app)
    app.state.redis = redis
    app.state.graph = FakeGraph()

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.include_router(chat_router)
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client, app, session_factory

    close_redis()
    engine.dispose()


def _token(app, uid=1):
    return security.issue_access_only(uid, "13800000001", "user")


def _ask(client, app, uid=1, query_user_id=None, with_token=True):
    params = {"question": "头疼怎么办", "session_id": "sid-001"}
    if query_user_id is not None:
        params["user_id"] = query_user_id
    headers = {}
    if with_token:
        headers["Authorization"] = f"Bearer {_token(app, uid)}"
    return client.get("/chat/agent", params=params, headers=headers)


# ---------------- 鉴权基础链路 ----------------

def test_no_token_no_query_rejected(client_fixture):
    client, app, _ = client_fixture
    resp = _ask(client, app, with_token=False)
    assert resp.status_code == 401
    assert resp.json()["code"] == BIZ_CODE.UNAUTHORIZED


def test_valid_token_uses_token_identity(client_fixture):
    client, app, _ = client_fixture
    resp = _ask(client, app, uid=123)
    assert resp.status_code == 200
    graph = app.state.graph
    assert graph.calls[-1]["state"]["user_id"] == 123
    assert graph.calls[-1]["config"]["configurable"]["thread_id"] == "user-123-sid-001"


def test_query_user_id_forgery_ignored(client_fixture):
    client, app, _ = client_fixture
    resp = _ask(client, app, uid=123, query_user_id="999")
    assert resp.status_code == 200
    assert app.state.graph.calls[-1]["state"]["user_id"] == 123  # 令牌身份优先


def test_garbage_token_rejected(client_fixture):
    client, app, _ = client_fixture
    resp = client.get(
        "/chat/agent",
        params={"question": "hi", "session_id": "s", "user_id": "1"},
        headers={"Authorization": "Bearer not.a.jwt"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == BIZ_CODE.TOKEN_INVALID


# ---------------- 兼容通道与止血开关 ----------------

def test_legacy_query_compat_passes_with_warning(client_fixture, caplog):
    client, app, _ = client_fixture
    with caplog.at_level(logging.WARNING, logger="yibot.api.auth"):
        resp = _ask(client, app, with_token=False, query_user_id="5")
    assert resp.status_code == 200
    assert app.state.graph.calls[-1]["state"]["user_id"] == 5
    assert any("legacy_user_id" in str(r.exc_text or "") or
               getattr(r, "event", "") == "legacy_user_id_compat"
               for r in caplog.records)


def test_auth_disabled_emergency_bypass(client_fixture, env):
    client, app, _ = client_fixture
    env.setenv("AUTH_ENABLED", "false")
    security.get_settings.cache_clear()
    resp = _ask(client, app, with_token=False, query_user_id="7")
    assert resp.status_code == 200
    assert app.state.graph.calls[-1]["state"]["user_id"] == 7


# ---------------- 数据隔离 ----------------

def test_session_history_isolated(client_fixture):
    client, app, session_factory = client_fixture
    db = session_factory()
    db.add(Session(session_id="sid-002", user_id="2", status="active"))
    db.commit()
    db.close()

    # 用户 1 的令牌访问用户 2 的会话历史 → 拒绝
    resp = client.get(
        "/chat/session/history",
        params={"session_id": "sid-002"},
        headers={"Authorization": f"Bearer {_token(app, uid=1)}"},
    )
    assert resp.status_code in (403, 404)
