import logging
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage

from model.provider import chat_model
from tools.tavily import tavily_search
from tools.custom_tools import *
logger = logging.getLogger("model.simple_agent")

model = chat_model()

agent = create_agent(
    model,
    tools=[get_weather, calculate, get_time, tavily_search],
    middleware=[handle_tool_errors],
    system_prompt="你是一个乐于助人的智能助手，擅长使用工具解决问题")

def get_agent_response(question: str) -> str:
    # 使用 stream_mode="updates" 可以看到每一个步骤
    logger.info("=== Agent 执行过程追踪 ===\n")

    user_msg = HumanMessage(content=question)
    step = 0
    for chunk in agent.stream({"messages": user_msg}, stream_mode="updates"):
        step += 1
        logger.info(f"--- 步骤 {step} ---")
        for node_name, update in chunk.items():
            logger.info(f"节点: {node_name}")
            if "messages" in update:
                for msg in update["messages"]:
                    if hasattr(msg, "tool_calls") and msg.tool_calls:
                        for tc in msg.tool_calls:
                            logger.info(f"  → 请求调用工具: {tc['name']}({tc['args']})")
                    elif msg.type == "tool":
                        logger.info(f"  → 工具结果 [{msg.name}]: {msg.content}")
                    elif msg.type == "ai" and msg.content:
                        logger.info(f"  → AI 回复: {msg.content[:100]}")
                        return msg.content