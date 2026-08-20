from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import MessagesState

from llm.base_llm import chat_model
from tools.custom_tools import *

model = chat_model()

agent = create_agent(
    model,
    tools=[get_weather, calculate, get_time],
    middleware=[handle_tool_errors],
    system_prompt="你是一个乐于助人的智能助手，擅长使用工具解决问题")

def chit_chat(state: MessagesState) -> dict:
    last_msg = state["messages"][-1].content
    print(f"=== chit chat Agent start, question: {last_msg} ===")

    user_msg = HumanMessage(content=last_msg)
    response = agent.invoke({"messages": user_msg})

    # print(f"=======> chit_chat response: {response}")
    if not response or not response["messages"]:
        return {"messages": [AIMessage(content="对不起，暂时无法回答你的问题")]}
    
    # for i, msg in enumerate(response["messages"]):
    #     print(f"\n[{i+1}] {msg.type}: {msg.content[:150]}...")
    
    ai_response = response["messages"][-1]

    total_tokens = 0
    metadata = ai_response.response_metadata
    if metadata and metadata["token_usage"] and metadata["token_usage"]["total_tokens"]:
        total_tokens = metadata["token_usage"]["total_tokens"]

    if total_tokens > 0:
        ai_msg = ai_response.content + f"\n\n【消耗{total_tokens}个token】"
    else:
        ai_msg = ai_response.content
    
    print("=== chit chat Agent end ===")
    return {"messages": [{"role": "assistant", "content": ai_msg}]}