import asyncio
import nest_asyncio
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import MessagesState

from llm.base_llm import chat_model
from tools.custom_tools import *
from tools.mcp import *

model = chat_model()

def chit_chat(state: MessagesState) -> dict:
    last_msg = state["messages"][-1].content
    print(f"=== chit chat Agent start, question: {last_msg} ===")

    # 获取MCP工具列表
    nest_asyncio.apply()
    loop = asyncio.get_event_loop()
    print("========> loop start")
    mcp_tools = loop.run_until_complete(get_mcp_tools())
    print("========> loop end")
    
    # 检查 mcp_tools 是否为列表
    if not isinstance(mcp_tools, list):
        print(f"警告：MCP工具获取失败，收到类型: {type(mcp_tools)}, 内容: {mcp_tools}")
        mcp_tools = []
    
    # 检查工具列表是否为空
    if not mcp_tools:
        print("警告：没有可用的MCP工具，将使用基本模式运行")
        mcp_tools = []

    agent = create_agent(
        model,
        mcp_tools,
        # middleware=[handle_tool_errors],
        system_prompt="你是一个乐于助人的智能助手，擅长分析当前问题，分解复杂的步骤并决定每一步需要使用到的工具以及工具间的执行顺序，然后解决问题")

    user_msg = HumanMessage(content=last_msg)
    response = agent.invoke({"messages": user_msg})

    print(f"=======> chit_chat response: {str(response)}")
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