"""T9：鉴权接口集成测试（spec: access-auth / design: 接口契约）。

场景：登录即注册、凭据错误不区分存在性、refresh 轮换、logout 吊销、
Redis 白名单写失败降级、响应无密码字段、旧接口脱敏。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.responses import BIZ_CODE, TraceIdMiddleware, register_exception_handlers
from api.routes import auth as auth_routes
from api.user import router as user_router
from database.conn import Base
from database.models.user import User


@pytest.fixture
def client_fixture(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-access-secret")
    monkeypatch.setenv("JWT_REFRESH_SECRET", "test-refresh-secret")
    from config.settings import get_settings

    get_settings.cache_clear()

    # StaticPool：内存库单连接共享（TestClient 跨线程 + 会话间数据持续可见）
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    User.__table__.c.id.type = BigInteger().with_variant(Integer(), "sqlite")
    Base.metadata.create_all(engine, tables=[User.__table__])
    session_factory = sessionmaker(bind=engine)

    from data.redis_client import close_redis, init_redis

    redis = init_redis(fake=True)

    app = FastAPI()
    app.add_middleware(TraceIdMiddleware)
    register_exception_handlers(app)
    app.state.redis = redis

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.include_router(auth_routes.router)
    app.include_router(user_router)
    app.dependency_overrides[auth_routes.get_db] = override_get_db

    with TestClient(app) as test_client:
        yield test_client, session_factory, redis

    close_redis()
    get_settings.cache_clear()
    engine.dispose()


def _login(client, mobile="13811110001", password="Pw123456"):
    return client.post("/user/login", json={"mobile": mobile, "password": password})


# ---------------- 登录（登录即注册） ----------------

def test_login_registers_new_user(client_fixture):
    client, _, _ = client_fixture
    resp = _login(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["trace_id"]
    data = body["data"]
    assert data["access_token"] and data["refresh_token"]
    assert data["expires_in"] == 30 * 60
    # 响应与用户信息均无任何密码字段（CR 红线）
    assert "password" not in str(data)
    assert data["user"]["uid"]
    assert data["user"]["mobile"] == "13811110001"


def test_login_existing_user_wrong_password(client_fixture):
    client, _, _ = client_fixture
    assert _login(client).status_code == 200
    resp = _login(client, password="Wrong0000")
    assert resp.status_code == 401
    body = resp.json()
    assert body["code"] == BIZ_CODE.BAD_CREDENTIALS


def test_disabled_login_uniform_error(client_fixture):
    """禁用账号登录：与密码错误同口径（401 手机号或密码错误），不暴露禁用状态。"""
    client, session_factory, _ = client_fixture
    from core.security import hash_password as _hash

    db = session_factory()
    db.add(User(name="禁用", mobile="13811110004", password="", hash_pwd=_hash("Right123"), disabled=1))
    db.commit()
    db.close()

    resp = _login(client, mobile="13811110004", password="Right123")
    assert resp.status_code == 401
    body = resp.json()
    assert body["code"] == BIZ_CODE.BAD_CREDENTIALS
    assert "禁用" not in body["msg"]


def test_login_plain_user_upgraded(client_fixture):
    """存量明文用户登录成功且完成哈希升级。"""
    client, session_factory, _ = client_fixture
    db = session_factory()
    db.add(User(name="存量", mobile="13811110002", password="legacy-pw", disabled=0))
    db.commit()
    db.close()

    resp = _login(client, mobile="13811110002", password="legacy-pw")
    assert resp.status_code == 200

    db = session_factory()
    fresh = db.query(User).filter(User.mobile == "13811110002").first()
    assert fresh.hash_pwd and not fresh.password
    db.close()


def test_login_invalid_mobile_rejected(client_fixture):
    client, _, _ = client_fixture
    resp = _login(client, mobile="12345")
    assert resp.status_code == 422


# ---------------- refresh 轮换 ----------------

def test_refresh_rotation_and_single_use(client_fixture):
    client, _, _ = client_fixture
    pair = _login(client).json()["data"]

    resp = client.post(
        "/user/token/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert resp.status_code == 200
    new_pair = resp.json()["data"]
    assert new_pair["access_token"] != pair["access_token"]
    assert new_pair["refresh_token"] != pair["refresh_token"]

    # 旧 refresh 单次有效
    again = client.post(
        "/user/token/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert again.status_code == 401


# ---------------- logout 吊销 + quota 鉴权探测 ----------------

def test_logout_revokes_access(client_fixture):
    client, _, _ = client_fixture
    pair = _login(client).json()["data"]

    resp = client.post(
        "/user/logout",
        headers={"Authorization": f"Bearer {pair['access_token']}"},
        json={"refresh_token": pair["refresh_token"]},
    )
    assert resp.status_code == 200

    # 登出后旧 access 被拒（/user/quota 已挂鉴权 → 401）
    blocked = client.get(
        "/user/quota",
        headers={"Authorization": f"Bearer {pair['access_token']}"},
    )
    assert blocked.status_code == 401


# ---------------- 降级：Redis 白名单写失败 ----------------

def test_login_degrades_when_redis_write_fails(client_fixture):
    client, _, redis = client_fixture

    class FailingSetRedis:
        def __getattr__(self, name):
            if name == "set":
                async def _fail(*args, **kwargs):
                    raise RuntimeError("redis down")
                return _fail
            return getattr(redis, name)

    original = auth_routes._request_redis
    auth_routes._request_redis = lambda request: FailingSetRedis()
    try:
        resp = _login(client)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["access_token"]  # access 正常颁发
        assert data["refresh_granted"] is False  # refresh 降级标记
    finally:
        auth_routes._request_redis = original


# ---------------- 旧接口脱敏 ----------------

def test_user_detail_mobile_sanitized(client_fixture):
    client, session_factory, _ = client_fixture
    db = session_factory()
    db.add(User(name="明文存量", mobile="13822220003", password="secret-pw", disabled=0))
    db.commit()
    db.close()

    resp = client.get("/user/detail/mobile", params={"mobile": "13822220003"})
    assert resp.status_code == 200
    payload = str(resp.json())
    assert "secret-pw" not in payload
    assert "hash_pwd" not in payload
