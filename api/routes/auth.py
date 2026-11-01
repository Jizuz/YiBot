"""鉴权路由（T9，spec: access-auth / design: 接口契约）。"""

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request

from api.deps.auth import get_current_user
from api.deps.db import get_db
from api.responses import BIZ_CODE, BizError, ok
from config.settings import get_settings
from core import security
from core.security import SecurityError
from schemas.auth_schema import LoginIn, LogoutIn, RefreshIn

logger = logging.getLogger("yibot.api.routes.auth")

router = APIRouter(prefix="/user", tags=["鉴权"])


def _request_redis(request: Request):
    return getattr(request.app.state, "redis", None) or security.get_redis()


def _user_info(user) -> dict:
    return {
        "uid": user.id,
        "name": user.name,
        "mobile": user.mobile,
        "role": "user",
    }


@router.post("/login")
async def login(body: LoginIn, request: Request, db=Depends(get_db)):
    """登录（登录即注册）：双令牌颁发；Redis 白名单写失败时降级仅发 access。"""
    from database import user_repository

    user = user_repository.authenticate(db, body.mobile, body.password)
    if user is None:
        existing = user_repository.query_user_by_mobile(db, body.mobile)
        if existing is None:
            user = user_repository.create_user_hashed(
                db, mobile=body.mobile, password=body.password
            )
            logger.info(
                "login auto-register", extra={"event": "auto_register", "uid": user.id}
            )
        else:
            # 存在但密码错误/禁用：统一口径，不区分原因（防撞库探测）
            raise BizError(BIZ_CODE.BAD_CREDENTIALS, "手机号或密码错误")

    redis = _request_redis(request)
    try:
        pair = await security.issue_token_pair(
            user.id, user.mobile, "user", redis
        )
    except Exception as exc:  # noqa: BLE001 - Redis 抖动不阻断登录（design 降级）
        logger.warning(
            "refresh whitelist write failed, degrade to access-only: %s",
            exc,
            extra={"event": "login_degrade", "uid": user.id},
        )
        token = security.issue_access_only(user.id, user.mobile, "user")
        return ok({
            "access_token": token,
            "refresh_token": "",
            "token_type": "bearer",
            "expires_in": get_settings().JWT_ACCESS_TTL_MIN * 60,
            "refresh_granted": False,
            "user": _user_info(user),
        })

    pair.pop("_refresh_jti", None)
    pair.pop("_refresh_exp", None)
    pair["user"] = _user_info(user)
    return ok(pair)


@router.post("/token/refresh")
async def refresh_token(body: RefreshIn, request: Request):
    """续期：refresh 白名单原子轮换（单次有效）。"""
    redis = _request_redis(request)
    try:
        pair = await security.rotate_refresh(
            body.refresh_token, redis, mobile="", role="user"
        )
    except SecurityError as exc:
        raise HTTPException(status_code=401, detail=exc.message) from exc
    pair.pop("_refresh_jti", None)
    pair.pop("_refresh_exp", None)
    pair.pop("user", None)
    return ok(pair)


@router.post("/logout")
async def logout(
    body: LogoutIn,
    request: Request,
    current=Depends(get_current_user),
):
    """登出：access 入黑名单 + refresh 白名单删除。"""
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

    credentials: HTTPAuthorizationCredentials | None = None
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=auth_header[7:]
        )
    token = credentials.credentials if credentials else None
    redis = _request_redis(request)
    await security.revoke_tokens(token, body.refresh_token, redis)
    return ok(None, msg="已登出")


@router.get("/quota")
async def quota(current=Depends(get_current_user)):
    """当日配额查询（T18 接入真实计量，当前占位结构）。"""
    settings = get_settings()
    quota_value = settings.LLM_DAILY_TOKEN_QUOTA
    return ok({
        "date": date.today().isoformat(),
        "used": 0,
        "quota": quota_value,
        "remaining": quota_value,
    })


# 兼容引用（BIZ_CODE 供异常处理器映射使用，保留显式导入便于阅读）
_ = BIZ_CODE
