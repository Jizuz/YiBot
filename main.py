# 创建FastAPI应用实例
from fastapi import FastAPI
from fastapi.concurrency import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware

from orchestration.supervisor import build_graph
from api.chat import router as ChatRouter
from api.user import router as UserRouter
from api.rag import router as RagRouter
from api.routes.auth import router as AuthRouter
from api.responses import TraceIdMiddleware, register_exception_handlers

from core.logging import setup_logging
from data.redis_client import close_redis, init_redis
from data.mysql.conn import create_checkpointer
from model.callbacks.token_meter import start_meter_worker, stop_meter_worker
from tools.mcp_gateway.gateway import mcp_gateway

app_state = {}

setup_logging()

@asynccontextmanager
async def lifespan(app: FastAPI):
    checkpointer, pool = await create_checkpointer()
    app.state.graph = build_graph(checkpointer)
    app.state.pool = pool
    app.state.redis = init_redis()
    # 计量常驻 worker：消费同步回调线程入队的 token 记录（跨 loop 安全）
    start_meter_worker()
    # MCP 网关: 订阅 Nacos 实例变化(熔断/LB) + 预热工具收敛缓存(失败不阻塞启动)
    await mcp_gateway.start()
    yield
    await mcp_gateway.stop()
    stop_meter_worker()
    close_redis()
    pool.close()
    await pool.wait_closed()

app = FastAPI(
    title="医疗Agent",
    version="1.0.0",
    description="这是一个智能医疗测试API，用于问诊咨询、挂号、开方等功能",
    terms_of_service="http://jk.cn",
    contact={
        "author": "jizuz",
        "email": "qpf123@outlook.com"
    },
    lifespan=lifespan,
)

app.add_middleware(TraceIdMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # 允许所有来源（P2 网关上线后收敛至网关统一 CORS）
    allow_credentials=True, # 允许携带身份凭证（如 Cookies）
    allow_methods=["*"], # 允许所有 HTTP 方法
    allow_headers=["*"], # 允许所有请求头
)

register_exception_handlers(app)

app.include_router(ChatRouter)
app.include_router(UserRouter)
app.include_router(RagRouter)
app.include_router(AuthRouter)

@app.get("/")
async def root():
    """根路径"""
    return {"message": "哈哈哈"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app="main:app", host="127.0.0.1", port=8000, reload=True)