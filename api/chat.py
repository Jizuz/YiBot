from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from llm.simple_agent import get_agent_response
from llm.simple_chat_model import get_ai_response
from llm.supervision import thinking_and_action

router = APIRouter(prefix="/chat", tags=["聊天管理"])

@router.get("/llm")
def common_chat(chats: str):
    """简单聊天接口"""
    response = get_ai_response(chats)
    return {"message": response}

@router.get("/agent")
async def common_ask_agent(question: str):
    """agent回答问题"""

    return StreamingResponse(
        thinking_and_action(question),
        media_type="text/event-stream",
    )


    