"""身份依赖：get_current_user（T10：令牌校验 + AUTH_ENABLED 开关 + query 兼容通道）。

- 身份一律以令牌 uid 为准（query/body user_id 仅作过渡期兼容回退，且必须告警）；
- AUTH_ENABLED=false：止血放行（身份回退 query user_id，仍需携带）；
- AUTH_LEGACY_QUERY_COMPAT=false：兼容通道彻底下线。
"""

import logging

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api.deps.db import get_db
from api.responses import BIZ_CODE, BizError
from core.security import SecurityError, verify_access
from data.redis_client import get_redis
from data.mysql import user_repository

logger = logging.getLogger("yibot.api.auth")

_bearer = HTTPBearer(auto_error=False)


def normalize_uid(raw):
    """uid 归一化：与 api/chat.py 既有逻辑一致（纯数字转 int，绑定 thread_id 格式）。"""
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return raw


def _legacy_identity(raw_user_id: str, reason: str) -> dict:
    logger.warning(
        "legacy query user_id used: %s（过渡期兼容通道，下线依赖此告警归零）",
        reason,
        extra={"event": "legacy_user_id_compat"},
    )
    return {
        "uid": normalize_uid(raw_user_id),
        "mobile": None,
        "role": "user",
        "compat": True,
    }


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db=Depends(get_db),
) -> dict:
    from config.settings import get_settings

    settings = get_settings()
    legacy_uid = request.query_params.get("user_id")

    if not settings.AUTH_ENABLED:
        # 止血开关：跳过令牌校验，身份仍需 query user_id 提供（无身份无法路由业务）
        if legacy_uid:
            return _legacy_identity(legacy_uid, "auth_disabled")
        raise BizError(BIZ_CODE.UNAUTHORIZED, "未登录")

    if credentials is None:
        if settings.AUTH_LEGACY_QUERY_COMPAT and legacy_uid:
            return _legacy_identity(legacy_uid, "no_token")
        raise BizError(BIZ_CODE.UNAUTHORIZED, "未登录")

    redis = getattr(request.app.state, "redis", None) or get_redis()
    try:
        payload = await verify_access(credentials.credentials, redis)
    except SecurityError as exc:
        raise BizError(exc.code, exc.message) from exc

    uid = normalize_uid(payload.get("uid"))
    # JWT 30min 窗口内的禁用即时生效：复核用户状态
    user = user_repository.query_user(db, uid) if isinstance(uid, int) else None
    if user is None and isinstance(uid, str):
        user = user_repository.query_user_by_mobile(db, payload.get("mobile", ""))
    if user is not None and user.disabled:
        raise BizError(BIZ_CODE.TOKEN_INVALID, "令牌无效")

    return {
        "uid": uid,
        "mobile": payload.get("mobile"),
        "role": payload.get("role", "user"),
        "compat": False,
    }

