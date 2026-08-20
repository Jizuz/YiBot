from langchain.agents import create_agent
from langchain_core.messages import HumanMessage

from langgraph.graph import StateGraph, START, END, MessagesState

from llm.sub_agent import purchase_agent
from llm.sub_agent import professional_agent
from llm.sub_agent import chit_chat_agent
from llm.base_llm import chat_model
from tools.custom_tools import *

model = chat_model()

agent = create_agent(
    model,
    system_prompt="你是一个优秀的工作流管理者，善于识别用户意图并将用户消息分发给特定智能体处理")

def classify_intent(state: MessagesState) -> str:
    """识别用户意图，根据意图路由到不同Agent"""
    last_message = state["messages"][-1]
    content = last_message.content.lower()

    if "解释" in content or "专业性" in content or "最新" in content:
        return "professional_agent"
    elif "想要" in content or "要买" in content or "购买" in content:
        return "purchase_agent"
    else:
        return "chit_chat_agent"

# 定义各个 Agent 节点
def router_node(state: MessagesState) -> dict:
    """路由节点：不做处理，只用于触发路由判断"""
    return {}

# 构建图
builder = StateGraph(MessagesState)

# 添加节点
builder.add_node("router", router_node)
builder.add_node("professional", professional_agent.ask_professional)
builder.add_node("purchase", purchase_agent.get_agent_response)
builder.add_node("chitchat", chit_chat_agent.chit_chat)

builder.add_edge(START, "router")
builder.add_conditional_edges(
    "router",
    classify_intent,
    {
        "professional_agent": "professional",
        "purchase_agent": "purchase",
        "chit_chat_agent": "chitchat"
    }
)
for node in ["professional", "purchase", "chitchat"]:
    builder.add_edge(node, END)

# 编译图
graph = builder.compile()

def thinking_and_action(question: str) -> str:
    user_msg = HumanMessage(content=question)
    result = graph.invoke({"messages": user_msg})

    if not result:
        return "无法回答您的问题"

    print(f"图调用结果: {result}")
    return result["messages"][-1].content