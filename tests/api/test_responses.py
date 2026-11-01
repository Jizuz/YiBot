"""T5：api/responses 统一响应与全局异常测试（spec: production-hardening）。"""

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.responses import (
    BIZ_CODE,
    TraceIdMiddleware,
    fail,
    ok,
    register_exception_handlers,
)


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(TraceIdMiddleware)
    register_exception_handlers(app)

    @app.get("/ok")
    def ok_route():
        return ok({"hello": "world"})

    @app.get("/http-error")
    def http_error():
        raise HTTPException(status_code=401, detail="token expired")

    @app.get("/boom")
    def boom():
        raise RuntimeError("internal stack secret")

    return app


client = TestClient(_build_app(), raise_server_exceptions=False)


def test_ok_structure_and_trace_id():
    resp = client.get("/ok")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["msg"] == "ok"
    assert body["data"] == {"hello": "world"}
    assert body["trace_id"]
    assert resp.headers["X-Request-Id"] == body["trace_id"]


def test_trace_id_passthrough_from_header():
    resp = client.get("/ok", headers={"X-Request-Id": "custom-trace-9"})
    assert resp.json()["trace_id"] == "custom-trace-9"


def test_http_exception_mapped():
    resp = client.get("/http-error")
    assert resp.status_code == 401
    body = resp.json()
    assert body["code"] == BIZ_CODE.TOKEN_INVALID
    assert body["msg"]
    assert body["trace_id"]


def test_unhandled_exception_sanitized():
    resp = client.get("/boom")
    assert resp.status_code == 500
    body = resp.json()
    assert body["code"] == BIZ_CODE.INTERNAL_ERROR
    assert "secret" not in body["msg"]
    assert "RuntimeError" not in body["msg"]


def test_fail_structure():
    body = fail(BIZ_CODE.QUOTA_EXCEEDED, "今日额度已用完")
    assert body["code"] == BIZ_CODE.QUOTA_EXCEEDED
    assert body["msg"] == "今日额度已用完"
    assert body["data"] is None
