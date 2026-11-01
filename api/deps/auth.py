"""身份依赖：get_current_user（T9 最小版；T10 扩展开关与兼容通道）。"""

import logging

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api.deps.db import get_db
from core.security import SecurityError, verify_access
from data.redis_client import get_redis
from database import user_repository

logger = logging.getLogger("yibot.api.auth")

_bearer = HTTPBearer(auto_error=False)


def normalize_uid(raw):
    """uid 归一化：与 api/chat.py 既有逻辑一致（纯数字转 int，绑定 thread_id 格式）。"""
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return raw


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db=Depends(get_db),
) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="未登录")
    redis = getattr(request.app.state, "redis", None) or get_redis()
    try:
        payload = await verify_access(credentials.credentials, redis)
    except SecurityError as exc:
        raise HTTPException(status_code=401, detail=exc.message) from exc

    uid = normalize_uid(payload.get("uid"))
    # JWT 30min 窗口内的禁用即时生效：按 uid 复核（uid 可能是 int 或 str）
    user = user_repository.query_user(db, uid) if isinstance(uid, int) else None
    if user is None and isinstance(uid, str):
        user = user_repository.query_user_by_mobile(db, payload.get("mobile", ""))
    if user is not None and user.disabled:
        raise HTTPException(status_code=401, detail="账号已禁用")

    return {
        "uid": uid,
        "mobile": payload.get("mobile"),
        "role": payload.get("role", "user"),
    }
