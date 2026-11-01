import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from api.deps.auth import get_current_user
from api.responses import BIZ_CODE, BizError
from config.settings import get_settings
from core import quota
from model.simple_chat_model import get_ai_response
from data.session_manager import SessionManager
from model.callbacks.token_meter import reset_meter_uid, set_meter_uid
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger("yibot.api.chat")

router = APIRouter(prefix="/chat", tags=["聊天管理"])


def _assert_session_owner_sync(session_id: str, uid) -> object | None:
    """属主校验（同步，供 threadpool 调用）：返回会话信息或 None（非属主/不存在）。"""
    info = SessionManager.repo.get_session_info(session_id)
    if info is None or str(info.user_id) != str(uid):
        return None
    return info


async def _assert_session_owner(session_id: str, uid) -> None:
    """数据隔离（spec: access-auth 权限与数据隔离）：仅会话属主可访问。"""
    info = await run_in_threadpool(_assert_session_owner_sync, session_id, uid)
    if info is None:
        raise BizError(40404, "会话不存在", status_code=404)


def _load_session_list_sync(user_id: str, page: int, page_size: int) -> dict:
    """会话列表组装（同步，供 threadpool 调用；逻辑自原 SessionManager 平移）。"""
    repo = SessionManager.repo
    offset = (page - 1) * page_size
    total = repo.count_session_list(user_id)
    session_list = repo.get_session_list(user_id, offset, page_size)

    records = []
    for item in session_list:
        message = repo.get_user_session_first_message(item.session_id)
        records.append({
            "sessionId": item.session_id,
            "status": item.status,
            "createTime": str(item.create_time),
            "lastActiveTime": str(item.last_active_time),
            "closeTime": str(item.close_time) if item.close_time else None,
            "abstract": message if message else "---",
        })

    return {"total": total, "page": page, "pageSize": page_size, "records": records}


def _load_playback_sync(session_id: str):
    """会话回放数据（同步，供 threadpool 调用；逻辑自原 SessionManager 平移）。"""
    repo = SessionManager.repo
    info = repo.get_session_info(session_id)
    if not info:
        return None
    msg_list = repo.get_session_message_list(session_id)
    return {
        "sessionInfo": {
            "sessionId": info.session_id,
            "status": info.status,
            "createTime": str(info.create_time),
        },
        "messageList": [
            {
                "userContent": m.content if m.type == 0 else None,
                "agentContent": m.content if m.type == 1 else None,
                "createTime": str(m.create_time),
            }
            for m in msg_list
        ],
    }


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

    # ---- LLM 配额前置拦截（spec: llm-token-quota；必须在进图之前）----
    settings = get_settings()
    redis = getattr(request.app.state, "redis", None)
    if settings.QUOTA_ENFORCE and redis is not None:
        allowed, _used, _remaining = await quota.check(
            redis, uid, settings.LLM_DAILY_TOKEN_QUOTA
        )
        if not allowed:
            logger.warning(
                "quota exceeded", extra={"event": "quota_blocked", "uid": uid}
            )
            raise BizError(
                BIZ_CODE.QUOTA_EXCEEDED,
                "今日咨询额度已用完，明天再来吧～（您也可以在个人页查看用量）",
            )

    graph = request.app.state.graph
    # thread_id 与 Checkpointer 存量数据绑定：格式 user-{uid}-{sid} 不可变更
    thread_id = f"user-{uid}-{session_id}"
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": settings.ORCH_RECURSION_LIMIT,  # 防失控循环（spec）
    }
    meter_token = set_meter_uid(uid)  # 全链路 LLM 调用计量身份
    try:
        result = await graph.ainvoke(
            {"messages": [{"role": "user", "content": question}], "user_id": uid, "session_id": session_id},
            config=config,
        )
    finally:
        reset_meter_uid(meter_token)
    last = result["messages"][-1]
    # 防御: 若图被静默中断(如 goto 未注册节点), 最后一条仍是用户消息, 不能原样回显
    if isinstance(last, HumanMessage) or not getattr(last, "content", None):
        reply = "抱歉，刚才的处理似乎中断了，请重新发送您的问题。"
    else:
        reply = last.content

    # ---- 剩余量预警话术（spec：剩余低于阈值随回复提示）----
    if redis is not None:
        _used, _quota_value, remaining = await quota.get_usage(
            redis, uid, settings.LLM_DAILY_TOKEN_QUOTA
        )
        if settings.LLM_DAILY_TOKEN_QUOTA > 0 and (
            remaining / settings.LLM_DAILY_TOKEN_QUOTA
            < (1 - settings.QUOTA_WARN_RATIO)
        ):
            reply = f"{reply}\n\n（提示：今日咨询额度剩余不足，请合理安排提问～）"

    return {
        "reply": reply,
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
async def save_chat_message(req: ChatMessageReq, current=Depends(get_current_user)):
    """提交对话消息，自动缓存+异步落库"""
    uid = current["uid"]
    logger.info("chat message save", extra={"event": "message_save", "uid": uid})
    try:
        # 自动获取当前会话
        session_id = await SessionManager.get_or_create_session(str(uid))
        # 保存消息
        await SessionManager.append_chat_message(session_id, req.type, req.content)
        return {"code": 0, "msg": "success", "data": {"session_id": session_id}}
    except Exception as e:
        logger.exception("message save failed", extra={"event": "message_save_fail", "uid": uid})
        return {"code": -1, "msg": str(e), "data": None}


@router.post("/session/close")
async def close_user_session(req: SessionOperateReq, current=Depends(get_current_user)):
    """主动关闭会话（清空缓存+更新数据库状态）——仅属主可关闭"""
    uid = current["uid"]
    if not req.session_id:
        return {"code": -1, "msg": "session_id不能为空", "data": None}
    await _assert_session_owner(req.session_id, uid)
    logger.info("chat session close", extra={"event": "session_close", "uid": uid})
    try:
        await SessionManager.close_session(req.session_id, str(uid))
        return {"code": 0, "msg": "success", "data": None}
    except Exception as e:
        logger.exception("session close failed", extra={"event": "session_close_fail", "uid": uid})
        return {"code": -1, "msg": str(e), "data": None}


@router.get("/session/list")
async def get_user_session_list_api(
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
        res_data = await run_in_threadpool(
            _load_session_list_sync, str(uid), page, page_size
        )
        return {"code": 0, "msg": "success", "data": res_data}
    except Exception as e:
        logger.exception("session list failed", extra={"event": "session_list_fail", "uid": uid})
        return {"code": -1, "msg": str(e), "data": None}


@router.get("/session/history")
async def get_session_history(
    session_id: str = Query(..., description="会话ID"),
    current=Depends(get_current_user),
):
    """查询会话历史记录（仅属主可查，React 前端历史展示专用）"""
    await _assert_session_owner(session_id, current["uid"])
    try:
        res_data = await run_in_threadpool(_load_playback_sync, session_id)
        if not res_data:
            return {"code": 404, "msg": "会话不存在", "data": None}
        return {"code": 0, "msg": "success", "data": res_data}
    except Exception as e:
        logger.exception("session history failed", extra={"event": "session_history_fail"})
        return {"code": -1, "msg": str(e), "data": None}
