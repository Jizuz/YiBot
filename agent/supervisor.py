from langchain_core.messages import AIMessage, SystemMessage

from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.types import Command

from agent.struct.consult_state import ConsultState
from agent.struct.schemas import SupervisorDecision
from agent.sub_agent.education_agent import education_agent_node
from agent.sub_agent.rights_agent import rights_agent_node
from agent.sub_agent.symptom_agent import symptom_agent_node
from agent.sub_agent.triage_agent import triage_agent_node
from llm.base_llm import chat_model
from tools.custom_tools import *

model = chat_model()

SUPERVISOR_PROMPT = """你是一个医疗问诊系统的调度主管。
你管理以下子 Agent：

- triage_agent：分诊与紧急程度评估。当用户描述症状、询问该挂什么科时调用。
- symptom_agent：结构化症状采集。当需要追问症状细节（部位、性质、持续时间）时调用。
- education_agent：健康科普。当用户询问疾病知识、检查解读、生活注意事项时调用。
- rights_agent：权益查询使用。当用户诉求查询权益、使用权益相关事项时调用。

你的职责：
1. 判断用户当前意图，选择最合适的子 Agent
2. 信息足够分诊则直接调 triage_agent；信息模糊先调 symptom_agent
3. symptom_agent 采集完成后通常需要再调 triage_agent
4. 子 Agent 已完成且用户无后续问题，返回 FINISH
5. 不要在对话中直接回答医学问题

协作关系：
- 如果用户描述的信息已经足够分诊，直接调用 triage_agent
- 如果信息模糊、需要追问细节，先调用 symptom_agent
- symptom_agent 采集完成后，通常需要再调用 triage_agent 给出分诊结论

当前活跃子 Agent：{active_agent}
"""

async def supervisor_node(state: ConsultState) -> Command:
    # 硬路由：症状采集刚完成，强制去分诊
    if state.get("pending_triage"):
        return Command(
            goto="triage_agent",
            update={
                "pending_triage": False,       # 消费掉标记
                "next_agent": "triage_agent",
                "active_agent": "triage_agent",
            },
        )

    # 硬路由：权益预约流程进行中(槽位未清空)，用户本轮回复大概率是流程输入(补信息/选门店/取消)，
    # 无条件回 rights_agent；若本轮内容与预约无关，rights_agent 会自动回落到查询流程处理
    if state.get("rights_booking"):
        return Command(
            goto="rights_agent",
            update={
                "next_agent": "rights_agent",
                "active_agent": "rights_agent",
                "agent_turn_count": 0,
            },
        )

    # 第一次进入时没有 active_agent
    active = state.get("active_agent", "无")

    decision = await model.with_structured_output(SupervisorDecision).ainvoke([
        SystemMessage(content=SUPERVISOR_PROMPT.format(active_agent=active)), *state["messages"],
    ])
    
    if decision.route == "FINISH":
        # 结束：由最终回复节点收尾
        return Command(goto="final_response", update={"next_agent": None})
    
    # 路由到子 Agent，重置该 Agent 的轮次
    return Command(
        goto=decision.route,
        update={
            "next_agent": decision.route,
            "active_agent": decision.route,
            "agent_turn_count": 0,
        },
    )

async def emergency_response_node(state: ConsultState) -> dict:
    return {"messages": [AIMessage(content=(
        "🚨 您的情况可能需要紧急处理，请立即拨打120或前往最近医院急诊科，不要延误。"
    ))]}


async def final_response_node(state: ConsultState) -> dict:
    resp = await model.ainvoke([
        SystemMessage(content="基于以上对话，给用户一个简洁的最终回复。"),
        *state["messages"],
    ])
    return {"messages": [resp]}

# 定义各个 Agent 节点
def router_node(state: MessagesState) -> dict:
    """路由节点：不做处理，只用于触发路由判断"""
    return {}

# 添加节点
def build_graph(checkpointer):
    g = StateGraph(ConsultState)
    g.add_node("supervisor", supervisor_node)
    g.add_node("triage_agent", triage_agent_node)
    g.add_node("symptom_agent", symptom_agent_node)
    g.add_node("education_agent", education_agent_node)
    g.add_node("rights_agent", rights_agent_node)
    g.add_node("emergency_response", emergency_response_node)
    g.add_node("final_response", final_response_node)

    g.add_edge(START, "supervisor")
    g.add_edge("emergency_response", END)
    g.add_edge("final_response", END)

    return g.compile(checkpointer=checkpointer)

# # 构建图
# builder = StateGraph(MessagesState)

# # 编译图
# graph = builder.compile()

# def thinking_and_action(question: str) -> str:
#     user_msg = HumanMessage(content=question)
#     result = graph.invoke({"messages": user_msg})

#     if not result:
#         return "无法回答您的问题"

#     print(f"图调用结果: {result}")
#     return result["messages"][-1].content