from langchain_core.messages import AIMessage, SystemMessage
from langgraph.types import Command

from langgraph.types import Command
from orchestration.model_provider import get_orch_model as _get_model
from orchestration.struct.consult_state import ConsultState
from orchestration.struct.schemas import CollectionStatus
from orchestration.prompts.symptom import SYMPTOM_PROMPT, COLLECTION_JUDGE_PROMPT

model = _get_model()


async def symptom_agent_node(state: ConsultState) -> Command:
    """症状采集子 Agent，支持多轮追问。"""
    turn = state.get("agent_turn_count", 0)
    
    # 硬上限保护
    if turn >= 3:
        # 强制总结并返回 Supervisor
        summary = await model.ainvoke([
            SystemMessage(content="基于已有信息，生成一段简短的症状总结。"), *state["messages"],
        ])
        return Command(
            goto="supervisor",
            update={
                "messages": [AIMessage(content=f"症状采集完成：{summary.content}")],
                "active_agent": None,
                "agent_turn_count": 0,
                "pending_triage": True,
                "symptom_summary": summary.content
            },
        )
    
    # 正常追问
    resp = await model.ainvoke([
        SystemMessage(content=SYMPTOM_PROMPT.format(collected="见对话历史")), *state["messages"],
    ])

    try:
        # 判断是否还需要继续追问（可以用 LLM 判断，也可以用规则）
        need_more = await should_continue_collecting(state, resp, turn)
    except Exception:
        # 降级：达到轮数上限就总结，否则问一个通用问题
        if turn >= 2:
            need_more = CollectionStatus(decision="summarize", reason="fallback")
        else:
            need_more = CollectionStatus(
                decision="continue",
                next_question="能再详细描述一下您的症状吗？",
                reason="fallback",
            )

    if need_more and getattr(need_more, "decision", "continue") == "continue":
        # 追问：把问题发给用户，等待下一轮输入
        # 注意：这里不返回 Supervisor，而是直接结束当前轮，等待用户回复
        return Command(
            goto="__end__",  # 结束本轮，等待用户输入后重新进入 graph
            update={
                "messages": [resp],
                "agent_turn_count": turn + 1,
            },
        )
    else:
        # 采集完成，返回 Supervisor
        return Command(
            goto="supervisor",
            update={
                "messages": [resp],
                "active_agent": None,
                "agent_turn_count": 0,
                "pending_triage": True,
                "symptom_summary": resp.content
            },
        )


async def should_continue_collecting(
    state: ConsultState,
    agent_response,
    turn_count: int,
) -> CollectionStatus:
    """用 LLM 判断症状采集是否可以结束。"""    
    # 把当前 Agent 的回复也加入上下文，让判断器知道"已经问过什么"
    context_messages = list(state["messages"])
    if agent_response is not None:
        context_messages.append(agent_response)
    
    status = await model.with_structured_output(CollectionStatus).ainvoke([
        SystemMessage(content=COLLECTION_JUDGE_PROMPT),
        *context_messages,
        SystemMessage(content=f"当前已追问轮数：{turn_count}"),
    ])
    
    return status
