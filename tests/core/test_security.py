"""T7：core/security JWT 双令牌核心测试（spec: access-auth / design: 接口契约）。

场景矩阵（tasks.md T7 细分）：签发/校验、过期、篡改、type 混用（双密钥交叉）、
黑名单、refresh 白名单原子轮换、登出吊销、密钥缺失、bcrypt 属性。
"""

import asyncio

import pytest

from core import security
from data.redis_client import close_redis, init_redis


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-access-secret")
    monkeypatch.setenv("JWT_REFRESH_SECRET", "test-refresh-secret")
    security.get_settings.cache_clear()
    yield
    security.get_settings.cache_clear()
    close_redis()


@pytest.fixture
def redis_client():
    return init_redis(fake=True)


def _issue(redis_client, **kwargs):
    return asyncio.run(
        security.issue_token_pair(42, "13800000001", "user", redis_client, **kwargs)
    )


# ---------------- bcrypt ----------------

def test_password_hash_properties():
    hashed = security.hash_password("Plain123")
    assert hashed != "Plain123"
    assert hashed.startswith("$2")  # bcrypt 格式
    assert security.hash_password("Plain123") != hashed  # 随机盐：两次不同
    assert security.verify_password("Plain123", hashed)
    assert not security.verify_password("Wrong", hashed)


# ---------------- 签发与校验 ----------------

def test_issue_token_pair_structure(redis_client):
    pair = _issue(redis_client)
    assert pair["access_token"] and pair["refresh_token"]
    assert pair["expires_in"] == 30 * 60
    assert pair["token_type"] == "bearer"

    payload = asyncio.run(security.verify_access(pair["access_token"], redis_client))
    assert payload["uid"] == 42
    assert payload["mobile"] == "13800000001"
    assert payload["role"] == "user"
    assert payload["type"] == "access"


def test_refresh_whitelist_written(redis_client):
    pair = _issue(redis_client)

    async def _check():
        decode = security._decode(
            pair["refresh_token"], security.TOKEN_TYPE_REFRESH
        )
        return await redis_client.get(security.refresh_key(decode["jti"]))

    assert asyncio.run(_check()) == "42"


def test_expired_access_rejected(redis_client):
    pair = _issue(redis_client, access_ttl_min=-1)
    with pytest.raises(security.SecurityError) as exc_info:
        asyncio.run(security.verify_access(pair["access_token"], redis_client))
    assert exc_info.value.code == security.BIZ_CODE.TOKEN_EXPIRED


def test_tampered_access_rejected(redis_client):
    pair = _issue(redis_client)
    tampered = pair["access_token"][:-6] + "XXXXXX"
    with pytest.raises(security.SecurityError) as exc_info:
        asyncio.run(security.verify_access(tampered, redis_client))
    assert exc_info.value.code == security.BIZ_CODE.TOKEN_INVALID


def test_refresh_used_as_access_rejected(redis_client):
    pair = _issue(redis_client)
    with pytest.raises(security.SecurityError) as exc_info:
        asyncio.run(security.verify_access(pair["refresh_token"], redis_client))
    assert exc_info.value.code == security.BIZ_CODE.TOKEN_INVALID


# ---------------- 轮换与吊销 ----------------

def test_rotate_refresh_single_use(redis_client):
    pair = _issue(redis_client)

    async def _rotate():
        return await security.rotate_refresh(
            pair["refresh_token"], redis_client, mobile="13800000001", role="user"
        )

    first = asyncio.run(_rotate())
    assert first["access_token"] != pair["access_token"]
    assert first["refresh_token"] != pair["refresh_token"]

    # 旧 refresh 已作废（白名单 GETDEL 后二次轮换失败）
    with pytest.raises(security.SecurityError):
        asyncio.run(_rotate())


def test_revoke_blocks_both_tokens(redis_client):
    pair = _issue(redis_client)

    async def _revoke():
        await security.revoke_tokens(
            pair["access_token"], pair["refresh_token"], redis_client
        )

    asyncio.run(_revoke())
    with pytest.raises(security.SecurityError) as exc_info:
        asyncio.run(security.verify_access(pair["access_token"], redis_client))
    assert exc_info.value.code == security.BIZ_CODE.TOKEN_REVOKED

    with pytest.raises(security.SecurityError):
        asyncio.run(
            security.rotate_refresh(
                pair["refresh_token"], redis_client,
                mobile="13800000001", role="user",
            )
        )


def test_forged_token_rejected(redis_client):
    import jwt as pyjwt

    forged = pyjwt.encode(
        {"uid": 1, "type": "access", "jti": "x", "exp": 9999999999},
        "wrong-secret",
        algorithm="HS256",
    )
    with pytest.raises(security.SecurityError) as exc_info:
        asyncio.run(security.verify_access(forged, redis_client))
    assert exc_info.value.code == security.BIZ_CODE.TOKEN_INVALID


def test_missing_secret_raises(monkeypatch, redis_client):
    monkeypatch.setenv("JWT_SECRET", "")
    monkeypatch.setenv("JWT_REFRESH_SECRET", "")
    security.get_settings.cache_clear()
    with pytest.raises(RuntimeError):
        _issue(redis_client)
