from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import MessagesState

from tools.tavily import tavily_extract, tavily_search
from llm.base_llm import chat_model

model = chat_model()

PROFESSIONAL_PROMPT = """
你是一个具有思考与行动能力的权威信息咨询助手，擅长使用工具搜索互联网实时内容或提取网页内容回答用户专业性问题。

## 工作流程
- 分析当前问题，思考是否需要使用tavily查询或提取工具
- 选择适当工具，执行完成给出答案

## 重要提醒
- 如果不知道或者未查询到结果，直接回答‘未查询到相关信息’，不能编造答案
"""

agent = create_agent(
    model,
    tools=[tavily_search, tavily_extract],
    system_prompt=PROFESSIONAL_PROMPT
)

def ask_professional(state: MessagesState) -> dict:
    last_msg = state["messages"][-1].content
    print(f"=== professional agent start, question: {last_msg} ===")

    user_msg = HumanMessage(content=last_msg)
    response = agent.invoke({"messages": user_msg})

    # print(f"=======> professional response: {response}")
    if not response or not response["messages"]:
        return {"messages": [AIMessage(content="对不起，暂时无法回答你的问题")]}

    
    ai_response = response["messages"][-1]
    print(f"=======> professional ai response: {ai_response}")
    
    total_tokens = 0
    metadata = ai_response.response_metadata
    if metadata and metadata["token_usage"] and metadata["token_usage"]["total_tokens"]:
        total_tokens = metadata["token_usage"]["total_tokens"]

    print(f"=======> professional tokens: {total_tokens}")

    if total_tokens > 0:
        ai_msg = ai_response.content + f"\n\n【共消耗{total_tokens}个token】"
    else:
        ai_msg = ai_response.content
    
    print("=== professional agent end ===")
    return {"messages": [{"role": "assistant", "content": ai_msg}]}