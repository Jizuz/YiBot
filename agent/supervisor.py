from langchain_core.messages import AIMessage, RemoveMessage, SystemMessage

from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.types import Command

from agent.struct.consult_state import ConsultState
from agent.struct.memory import WINDOW_ROUNDS, background_block, fold_messages, recent_rounds
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

async def _maintain_global_memory(state: ConsultState) -> dict | None:
    """全局汇总记忆维护: 主信箱超出滚动窗口(WINDOW_ROUNDS 轮)时, 把旧消息折叠进 global_summary。

    任何一步失败都降级为本轮不折叠(旧消息暂留, 下轮再试), 不影响主流程。
    """
    msgs = state["messages"]
    keep = recent_rounds(msgs, WINDOW_ROUNDS)
    fold_part = msgs[: len(msgs) - len(keep)]
    if not fold_part:
        return None
    if not all(getattr(m, "id", None) for m in fold_part):
        return None  # 存在无 id 消息无法 Remove, 降级跳过
    new_summary = await fold_messages(model, fold_part, state.get("global_summary"))
    if new_summary is None:
        print("警告：全局记忆折叠失败，本轮跳过")
        return None
    print(f"=== Supervisor 全局记忆折叠：{len(fold_part)} 条旧消息 -> global_summary（保留最近 {len(keep)} 条） ===")
    return {"messages": [RemoveMessage(id=m.id) for m in fold_part], "global_summary": new_summary}


async def supervisor_node(state: ConsultState) -> Command:
    # 每轮最先执行: 维护全局汇总记忆(主信箱滚动窗口), 各返回路径统一 merge 该 update
    mem_update = await _maintain_global_memory(state)

    # 硬路由：症状采集刚完成，强制去分诊
    if state.get("pending_triage"):
        return Command(
            goto="triage_agent",
            update={
                **(mem_update or {}),
                "pending_triage": False,       # 消费掉标记
                "next_agent": "triage_agent",
                "active_agent": "triage_agent",
            },
        )

    # 硬路由：权益预约流程进行中(rights_memory.appointing 未清空)，用户本轮回复大概率是流程输入(补信息/选门店/取消)，
    # 无条件回 rights_agent；若本轮内容与预约无关，rights_agent 会自动回落到查询流程处理
    if (state.get("rights_memory") or {}).get("appointing"):
        return Command(
            goto="rights_agent",
            update={
                **(mem_update or {}),
                "next_agent": "rights_agent",
                "active_agent": "rights_agent",
                "agent_turn_count": 0,
            },
        )

    # 第一次进入时没有 active_agent
    active = state.get("active_agent", "无")

    # 路由输入: 全局汇总记忆(背景) + 最近 WINDOW_ROUNDS 轮原始消息, 不再携带全量历史
    decision = await model.with_structured_output(SupervisorDecision).ainvoke([
        SystemMessage(content=SUPERVISOR_PROMPT.format(active_agent=active)
                      + background_block(state.get("global_summary"))),
        *recent_rounds(state["messages"]),
    ])
    
    if decision.route == "FINISH":
        # 结束：由最终回复节点收尾
        return Command(goto="final_response", update={**(mem_update or {}), "next_agent": None})
    
    # 路由到子 Agent，重置该 Agent 的轮次
    return Command(
        goto=decision.route,
        update={
            **(mem_update or {}),
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
    # 收尾输入: 全局汇总记忆(背景) + 最近窗口轮次, 不再携带全量历史
    resp = await model.ainvoke([
        SystemMessage(content="基于以上对话，给用户一个简洁的最终回复。"
                      + background_block(state.get("global_summary"))),
        *recent_rounds(state["messages"]),
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