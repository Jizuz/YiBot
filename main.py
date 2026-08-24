# 创建FastAPI应用实例
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.chat import router as ChatRouter
from api.user import router as UserRouter

import sys
sys.dont_write_bytecode = True

app = FastAPI(
    title="医疗测试",
    version="1.0.0",
    description="这是一个智能医疗测试API，用于问诊咨询、挂号、开方等功能",
    terms_of_service="http://jk.cn",
    contact={
        "author": "jizuz",
        "email": "qpf123@outlook.com"
    }
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # 允许所有来源
    allow_credentials=True, # 允许携带身份凭证（如 Cookies）
    allow_methods=["*"], # 允许所有 HTTP 方法
    allow_headers=["*"], # 允许所有请求头
)

app.include_router(ChatRouter)
app.include_router(UserRouter)

@app.get("/")
async def root():
    """根路径"""
    return {"message": "哈哈哈"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app="main:app", host="127.0.0.1", port=8000, reload=True)