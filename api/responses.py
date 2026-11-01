"""统一响应与全局异常（T5，spec: production-hardening）。

- 统一结构：{"code": 业务码, "msg": 提示, "data": 数据, "trace_id": 链路标识}；
- 业务码段：0 成功 / 40101~40199 鉴权 / 42901 配额 / 50000 系统异常；
- TraceIdMiddleware：透传或生成 X-Request-Id，并写入响应头；
- 全局异常兜底：未捕获异常不外泄堆栈细节。
"""

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from core.logging import (
    get_trace_id,
    new_trace_id,
    reset_trace_id,
    set_trace_id,
)

logger = logging.getLogger("yibot.api")


class BIZ_CODE:
    OK = 0
    # 鉴权段 40100-40199
    UNAUTHORIZED = 40101      # 未登录
    TOKEN_EXPIRED = 40102     # 令牌过期
    TOKEN_INVALID = 40103     # 令牌无效（含篡改/类型错误/已轮换）
    TOKEN_REVOKED = 40104     # 已吊销
    BAD_CREDENTIALS = 40105   # 手机号或密码错误
    # 配额段 42900-42999
    QUOTA_EXCEEDED = 42901    # 当日额度用尽
    # 系统段
    INTERNAL_ERROR = 50000


_HTTP_STATUS_TO_BIZ = {
    401: BIZ_CODE.TOKEN_INVALID,
    403: BIZ_CODE.TOKEN_INVALID,
    404: BIZ_CODE.INTERNAL_ERROR,
    422: BIZ_CODE.INTERNAL_ERROR,
    429: BIZ_CODE.QUOTA_EXCEEDED,
}


def _payload(code: int, msg: str, data) -> dict:
    return {
        "code": code,
        "msg": msg,
        "data": data,
        "trace_id": get_trace_id() or "",
    }


def ok(data=None, msg: str = "ok") -> dict:
    """成功响应统一结构。"""
    return _payload(BIZ_CODE.OK, msg, data)


def fail(code: int, msg: str, data=None) -> dict:
    """失败响应统一结构。"""
    return _payload(code, msg, data)


class TraceIdMiddleware(BaseHTTPMiddleware):
    """透传/生成 trace_id，贯穿请求上下文并回写响应头。"""

    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get("X-Request-Id") or new_trace_id()
        token = set_trace_id(trace_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-Id"] = trace_id
            return response
        finally:
            reset_trace_id(token)


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常兜底：HTTPException 按状态映射业务码；未知异常脱敏。"""

    @app.exception_handler(HTTPException)
    async def on_http_exception(request: Request, exc: HTTPException):
        code = _HTTP_STATUS_TO_BIZ.get(exc.status_code, BIZ_CODE.INTERNAL_ERROR)
        return JSONResponse(
            status_code=exc.status_code,
            content=_payload(code, str(exc.detail), None),
        )

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content=_payload(BIZ_CODE.INTERNAL_ERROR, "请求参数校验失败", None),
        )

    @app.exception_handler(Exception)
    async def on_unhandled(request: Request, exc: Exception):
        logger.exception(
            "unhandled exception", extra={"event": "unhandled_exception"}
        )
        return JSONResponse(
            status_code=500,
            content=_payload(
                BIZ_CODE.INTERNAL_ERROR, "服务内部错误，请稍后重试", None
            ),
        )
