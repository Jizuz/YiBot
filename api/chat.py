from typing import Optional
from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from llm.simple_chat_model import get_ai_response
from manager.session_manager import SessionManager

router = APIRouter(prefix="/chat", tags=["聊天管理"])

@router.get("/llm")
def common_chat(chats: str):
    """简单聊天接口"""
    response = get_ai_response(chats)
    return {"message": response}

@router.get("/agent")
async def common_ask_agent(question: str, session_id: str, request: Request):
    """agent回答问题"""
    print('common_ask_agent start...')

    graph = request.app.state.graph
    thread_id = f"user-{session_id}"
    config = {"configurable": {"thread_id": thread_id}}
    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": question}], "session_id": session_id},
        config=config,
    )
    last = result["messages"][-1]
    return {
        "reply": last.content,
        "need_emergency": result.get("need_emergency", False),
        "active_agent": result.get("active_agent"),
    }

class ChatMessageReq(BaseModel):
    user_id: str
    type: int
    content: str

class SessionOperateReq(BaseModel):
    user_id: str
    session_id: Optional[str] = None

@router.post("/message/save")
def save_chat_message(req: ChatMessageReq):
    """提交对话消息，自动缓存+异步落库"""
    print(f"chat save_chat_message start")
    try:
        # 自动获取当前会话
        session_id = SessionManager.get_or_create_session(req.user_id)
        # 保存消息
        SessionManager.append_chat_message(session_id, req.type, req.content)
        return {"code": 0, "msg": "success", "data": {"session_id": session_id}}
    except Exception as e:
        return {"code": -1, "msg": str(e), "data": None}

@router.post("/session/close")
def close_user_session(req: SessionOperateReq):
    """主动关闭会话（清空缓存+更新数据库状态）"""
    print(f"chat close_user_session start")
    try:
        if not req.session_id:
            return {"code": -1, "msg": "session_id不能为空", "data": None}
        SessionManager.close_session(req.session_id, req.user_id)
        return {"code": 0, "msg": "success", "data": None}
    except Exception as e:
        return {"code": -1, "msg": str(e), "data": None}

@router.get("/session/list")
def get_user_session_list_api(
    user_id: str = Query(..., description="用户ID"),
    page: int = Query(1, description="页码"),
    page_size: int = Query(10, description="每页数量")
):
    """
    获取用户会话列表（分页）
    适配前端：我的历史会话列表页
    默认按最后活跃时间倒序，最新会话优先
    """
    try:
        res_data = SessionManager.get_user_session_list(user_id, page, page_size)
        return {"code": 0, "msg": "success", "data": res_data}
    except Exception as e:
        return {"code": -1, "msg": str(e), "data": None}

@router.get("/session/history")
def get_session_history(session_id: str = Query(..., description="会话ID")):
    """
    查询会话历史记录
    专供React前端静态展示历史会话使用
    """
    try:
        res_data = SessionManager.get_session_playback_data(session_id)
        if not res_data:
            return {"code": 404, "msg": "会话不存在", "data": None}
        return {"code": 0, "msg": "success", "data": res_data}
    except Exception as e:
        return {"code": -1, "msg": str(e), "data": None}