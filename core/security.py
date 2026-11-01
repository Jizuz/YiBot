"""JWT 双令牌与密码哈希核心（T7，spec: access-auth / design: 接口契约）。

- access：HS256/JWT_SECRET，payload 含 uid/mobile/role/type/jti/exp/iat；
- refresh：HS256/JWT_REFRESH_SECRET（双密钥分离，防令牌类型混用）；
- refresh 白名单 `auth:refresh:{jti}`（GETDEL 原子轮换，单次有效）；
- 登出吊销：access jti 入黑名单 `auth:deny:{jti}`（TTL=剩余有效期）；
- bcrypt 随机盐哈希（passlib）。
"""

import logging
import time
import uuid

import bcrypt
import jwt as pyjwt

from core.biz_codes import BIZ_CODE
from config.settings import get_settings

logger = logging.getLogger("yibot.core.security")

TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"

# bcrypt 成本因子（passlib 1.7.4 与 bcrypt 5.x 不兼容，故直接使用 bcrypt 库）
_BCRYPT_ROUNDS = 12


class SecurityError(Exception):
    """令牌校验失败（code 对应统一业务码段）。"""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def refresh_key(jti: str) -> str:
    return f"auth:refresh:{jti}"


def deny_key(jti: str) -> str:
    return f"auth:deny:{jti}"


# ---------------- bcrypt ----------------

def hash_password(plain: str) -> str:
    salt = bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
    return bcrypt.hashpw(plain.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


# ---------------- 签发与解码 ----------------

def _require_secrets() -> tuple[str, str]:
    settings = get_settings()
    if not settings.JWT_SECRET or not settings.JWT_REFRESH_SECRET:
        raise RuntimeError(
            "JWT_SECRET / JWT_REFRESH_SECRET 未配置：请检查环境变量"
        )
    return settings.JWT_SECRET, settings.JWT_REFRESH_SECRET


def _issue(
    token_type: str, secret: str, ttl_seconds: int, **claims
) -> tuple[str, str, int]:
    now = int(time.time())
    jti = uuid.uuid4().hex
    payload = {
        **claims,
        "type": token_type,
        "jti": jti,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    token = pyjwt.encode(payload, secret, algorithm="HS256")
    return token, jti, payload["exp"]


def _decode(token: str, expected_type: str) -> dict:
    settings = get_settings()
    secret = (
        settings.JWT_SECRET
        if expected_type == TOKEN_TYPE_ACCESS
        else settings.JWT_REFRESH_SECRET
    )
    try:
        payload = pyjwt.decode(
            token, secret, algorithms=["HS256"]  # 显式算法白名单，防算法混淆
        )
    except pyjwt.ExpiredSignatureError as exc:
        raise SecurityError(BIZ_CODE.TOKEN_EXPIRED, "令牌已过期") from exc
    except pyjwt.InvalidTokenError as exc:
        raise SecurityError(BIZ_CODE.TOKEN_INVALID, "令牌无效") from exc
    if payload.get("type") != expected_type or not payload.get("jti"):
        raise SecurityError(BIZ_CODE.TOKEN_INVALID, "令牌类型无效")
    return payload


def issue_access_only(uid: int | str, mobile: str, role: str) -> str:
    """仅签发 access（Redis 白名单写失败时的登录降级路径，不触碰 Redis）。"""
    access_secret, _ = _require_secrets()
    settings = get_settings()
    token, _, _ = _issue(
        TOKEN_TYPE_ACCESS, access_secret,
        settings.JWT_ACCESS_TTL_MIN * 60,
        uid=uid, mobile=mobile, role=role,
    )
    return token


async def issue_token_pair(
    uid: int | str,
    mobile: str,
    role: str,
    redis,
    access_ttl_min: int | None = None,
    refresh_ttl_days: int | None = None,
) -> dict:
    """签发双令牌并写入 refresh 白名单。"""
    access_secret, refresh_secret = _require_secrets()
    settings = get_settings()
    access_ttl = (access_ttl_min or settings.JWT_ACCESS_TTL_MIN) * 60
    refresh_ttl = (refresh_ttl_days or settings.JWT_REFRESH_TTL_DAYS) * 24 * 3600

    access_token, _, _ = _issue(
        TOKEN_TYPE_ACCESS, access_secret, access_ttl,
        uid=uid, mobile=mobile, role=role,
    )
    refresh_token, refresh_jti, refresh_exp = _issue(
        TOKEN_TYPE_REFRESH, refresh_secret, refresh_ttl, uid=uid,
    )
    await redis.set(refresh_key(refresh_jti), str(uid), ex=max(refresh_ttl, 1))
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": access_ttl,
        "refresh_granted": True,
        "_refresh_jti": refresh_jti,       # 内部字段（revoke 用，不序列化到响应）
        "_refresh_exp": refresh_exp,
    }


async def verify_access(token: str, redis) -> dict:
    """校验 access：签名 → exp → type → 黑名单。"""
    payload = _decode(token, TOKEN_TYPE_ACCESS)
    if await redis.get(deny_key(payload["jti"])):
        raise SecurityError(BIZ_CODE.TOKEN_REVOKED, "令牌已吊销")
    return payload


async def rotate_refresh(
    refresh_token: str, redis, *, mobile: str, role: str
) -> dict:
    """校验 refresh 白名单并原子轮换（GETDEL：单次有效，并发仅一次成功）。"""
    payload = _decode(refresh_token, TOKEN_TYPE_REFRESH)
    uid = await redis.getdel(refresh_key(payload["jti"]))
    if uid is None:
        raise SecurityError(BIZ_CODE.TOKEN_INVALID, "续期令牌已失效")
    return await issue_token_pair(
        payload["uid"] if payload.get("uid") is not None else uid,
        mobile, role, redis,
    )


async def revoke_tokens(
    access_token: str | None, refresh_token: str | None, redis
) -> None:
    """登出吊销：access jti 入黑名单（TTL=剩余有效期），refresh 白名单删除。"""
    if access_token:
        try:
            payload = _decode(access_token, TOKEN_TYPE_ACCESS)
            remaining = max(int(payload["exp"] - time.time()), 1)
            await redis.set(deny_key(payload["jti"]), "1", ex=remaining)
        except SecurityError:
            logger.info("revoke skip invalid access token", extra={"event": "revoke"})
    if refresh_token:
        try:
            payload = _decode(refresh_token, TOKEN_TYPE_REFRESH)
            await redis.delete(refresh_key(payload["jti"]))
        except SecurityError:
            logger.info("revoke skip invalid refresh token", extra={"event": "revoke"})
