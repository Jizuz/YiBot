from langchain_core.messages import AIMessage, SystemMessage
from langgraph.types import Command

from langgraph.types import Command
from llm.base_llm import chat_model
from agent.struct.consult_state import ConsultState
from agent.struct.schemas import CollectionStatus

model = chat_model()

SYMPTOM_PROMPT = """你是一个症状采集助手。你的任务是通过多轮对话，收集用户症状的以下维度：
1. 部位（哪里不舒服）
2. 性质（什么样的感觉：刺痛/胀痛/烧灼感等）
3. 持续时间（多久了）
4. 频率（持续/间歇）
5. 诱因/缓解因素

规则：
- 每次只问一个维度，不要一次问多个
- 如果用户已经提供了某些维度的信息，不要重复询问
- 当所有关键维度都收集完毕，输出一段结构化总结，然后返回主管
- 最多追问 3 轮，超过则基于已有信息总结

当前已收集的信息：{collected}
"""

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
        need_more = await should_continue_collecting(state, resp)
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

    if need_more:
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

COLLECTION_JUDGE_PROMPT = """你是一个症状采集进度判断器。

症状采集需要收集以下 5 个维度：
1. 部位（哪里不舒服）
2. 性质（什么样的感觉：刺痛/胀痛/烧灼感等）
3. 持续时间（多久了）
4. 频率（持续/间歇，发作次数）
5. 诱因/缓解因素（什么情况下加重或缓解）

请仔细阅读对话历史，判断：
- 哪些维度用户已经明确提供了？
- 哪些维度还缺失？
- 如果缺失的维度中包含"部位"或"性质"这两个核心维度，必须继续追问
- 如果核心维度已收集，但仍有 1-2 个次要维度缺失，且已经追问了 2 轮以上，可以总结
- 如果 5 个维度都收集齐了，可以总结

输出要求：
- collected：已收集维度及用户原话中的值
- missing：缺失的维度名称列表
- decision：continue 或 summarize
- next_question：如果 continue，生成一个自然的口语化问题，只问一个维度
- reason：简短说明判断理由
"""

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
