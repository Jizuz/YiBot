import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from api.deps.auth import get_current_user
from api.responses import BizError
from llm.simple_chat_model import get_ai_response
from manager.session_manager import SessionManager

logger = logging.getLogger("yibot.api.chat")

router = APIRouter(prefix="/chat", tags=["聊天管理"])


def _assert_session_owner(session_id: str, uid) -> None:
    """数据隔离（spec: access-auth 权限与数据隔离）：仅会话属主可访问。"""
    info = SessionManager.repo.get_session_info(session_id)
    if info is None or str(info.user_id) != str(uid):
        raise BizError(40404, "会话不存在", status_code=404)


@router.get("/llm")
def common_chat(chats: str, current=Depends(get_current_user)):
    """简单聊天接口（受保护）"""
    response = get_ai_response(chats)
    return {"message": response}


@router.get("/agent")
async def common_ask_agent(
    question: str,
    session_id: str,
    request: Request,
    current=Depends(get_current_user),
    user_id: Optional[str] = Query(
        None, deprecated=True,
        description="已废弃：身份一律取自令牌（过渡期兼容通道使用）",
    ),
):
    """agent回答问题（身份取自令牌，user_id 参数仅兼容保留并被忽略）"""
    uid = current["uid"]
    logger.info("chat agent start", extra={"event": "chat_agent", "uid": uid})

    graph = request.app.state.graph
    # thread_id 与 Checkpointer 存量数据绑定：格式 user-{uid}-{sid} 不可变更
    thread_id = f"user-{uid}-{session_id}"
    config = {"configurable": {"thread_id": thread_id}}
    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": question}], "user_id": uid, "session_id": session_id},
        config=config,
    )
    last = result["messages"][-1]
    # 防御: 若图被静默中断(如 goto 未注册节点), 最后一条仍是用户消息, 不能原样回显
    if isinstance(last, HumanMessage) or not getattr(last, "content", None):
        return {
            "reply": "抱歉，刚才的处理似乎中断了，请重新发送您的问题。",
            "need_emergency": result.get("need_emergency", False),
            "active_agent": result.get("active_agent"),
        }
    return {
        "reply": last.content,
        "need_emergency": result.get("need_emergency", False),
        "active_agent": result.get("active_agent"),
    }


class ChatMessageReq(BaseModel):
    user_id: Optional[str] = None  # 已废弃：身份取自令牌（兼容旧前端请求体保留）
    type: int
    content: str


class SessionOperateReq(BaseModel):
    user_id: Optional[str] = None  # 已废弃：身份取自令牌
    session_id: Optional[str] = None


@router.post("/message/save")
def save_chat_message(req: ChatMessageReq, current=Depends(get_current_user)):
    """提交对话消息，自动缓存+异步落库"""
    uid = current["uid"]
    logger.info("chat message save", extra={"event": "message_save", "uid": uid})
    try:
        # 自动获取当前会话
        session_id = SessionManager.get_or_create_session(str(uid))
        # 保存消息
        SessionManager.append_chat_message(session_id, req.type, req.content)
        return {"code": 0, "msg": "success", "data": {"session_id": session_id}}
    except Exception as e:
        logger.exception("message save failed", extra={"event": "message_save_fail", "uid": uid})
        return {"code": -1, "msg": str(e), "data": None}


@router.post("/session/close")
def close_user_session(req: SessionOperateReq, current=Depends(get_current_user)):
    """主动关闭会话（清空缓存+更新数据库状态）——仅属主可关闭"""
    uid = current["uid"]
    if not req.session_id:
        return {"code": -1, "msg": "session_id不能为空", "data": None}
    _assert_session_owner(req.session_id, uid)
    logger.info("chat session close", extra={"event": "session_close", "uid": uid})
    try:
        SessionManager.close_session(req.session_id, str(uid))
        return {"code": 0, "msg": "success", "data": None}
    except Exception as e:
        logger.exception("session close failed", extra={"event": "session_close_fail", "uid": uid})
        return {"code": -1, "msg": str(e), "data": None}


@router.get("/session/list")
def get_user_session_list_api(
    current=Depends(get_current_user),
    page: int = Query(1, description="页码"),
    page_size: int = Query(10, description="每页数量"),
    user_id: Optional[str] = Query(
        None, deprecated=True, description="已废弃：身份取自令牌",
    ),
):
    """
    获取当前用户会话列表（分页，身份取自令牌）
    适配前端：我的历史会话列表页，按最后活跃时间倒序
    """
    uid = current["uid"]
    try:
        res_data = SessionManager.get_user_session_list(str(uid), page, page_size)
        return {"code": 0, "msg": "success", "data": res_data}
    except Exception as e:
        logger.exception("session list failed", extra={"event": "session_list_fail", "uid": uid})
        return {"code": -1, "msg": str(e), "data": None}


@router.get("/session/history")
def get_session_history(
    session_id: str = Query(..., description="会话ID"),
    current=Depends(get_current_user),
):
    """查询会话历史记录（仅属主可查，React 前端历史展示专用）"""
    _assert_session_owner(session_id, current["uid"])
    try:
        res_data = SessionManager.get_session_playback_data(session_id)
        if not res_data:
            return {"code": 404, "msg": "会话不存在", "data": None}
        return {"code": 0, "msg": "success", "data": res_data}
    except Exception as e:
        logger.exception("session history failed", extra={"event": "session_history_fail"})
        return {"code": -1, "msg": str(e), "data": None}
