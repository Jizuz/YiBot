import logging
from langchain_core.messages import AIMessage, RemoveMessage, SystemMessage

from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.types import Command

from orchestration.struct.consult_state import ConsultState
from orchestration.struct.memory import WINDOW_ROUNDS, background_block, fold_messages, recent_rounds
from orchestration.struct.schemas import SupervisorDecision
from orchestration.sub_agent.education_agent import education_agent_node
from orchestration.sub_agent.rights_agent import rights_agent_node
from orchestration.sub_agent.symptom_agent import symptom_agent_node
from orchestration.sub_agent.triage_agent import triage_agent_node
from orchestration.model_provider import get_orch_model as _get_model
from tools.custom_tools import *
from orchestration.prompts.supervisor import SUPERVISOR_PROMPT
logger = logging.getLogger("orchestration.supervisor")

model = _get_model()


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
        logger.info("警告：全局记忆折叠失败，本轮跳过")
        return None
    logger.info(f"=== Supervisor 全局记忆折叠：{len(fold_part)} 条旧消息 -> global_summary（保留最近 {len(keep)} 条） ===")
    return {"messages": [RemoveMessage(id=m.id) for m in fold_part], "global_summary": new_summary}


async def supervisor_node(state: ConsultState) -> Command:
    # 每轮最先执行: 维护全局汇总记忆(主信箱滚动窗口), 各返回路径统一 merge 该 update
    mem_update = await _maintain_global_memory(state)

    # 硬路由：education_agent 判定无法以科普满足(检索未覆盖/涉及个人情况/越界)，强制进入症状采集
    # (采集完成后由下方 pending_triage 硬路由接续分诊, 形成采集 -> 分诊完整链路)
    if state.get("pending_symptom_collect"):
        return Command(
            goto="symptom_agent",
            update={
                **(mem_update or {}),
                "pending_symptom_collect": False,   # 消费掉标记
                "next_agent": "symptom_agent",
                "active_agent": "symptom_agent",
                "agent_turn_count": 0,
            },
        )

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

#     logger.info(f"图调用结果: {result}")
#     return result["messages"][-1].content