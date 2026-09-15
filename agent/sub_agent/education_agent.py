from langchain_core.messages import AIMessage, SystemMessage
from langgraph.types import Command

from agent.struct.consult_state import ConsultState
from agent.struct.schemas import EducationResult
from llm.base_llm import chat_model
from rag.core.hybird import format_retrieved_context
from rag.core.hybird import HybridRetriever as retriever

model = chat_model()

EDUCATION_PROMPT = """你是健康科普助手，用通俗语言回答健康知识问题。

能回答：疾病知识、检查指标一般意义、生活方式建议。
不能回答：个人诊断、具体用药、解读个人报告下结论。

约束：基于检索资料，不编造；涉及个人情况提示咨询医生；超范围设 out_of_scope=true。
"""

OUT_OF_SCOPE_PATTERNS = [
    r"我(这|该|要)吃(什么|啥)药",
    r"帮我(看看|解读).*(报告|检查|化验)",
    r"我(是不是|得了|这是).*(病|癌|炎)",
    r"(给我|帮我).*(开药|处方|剂量)",
]

import re

def check_out_of_scope(text: str) -> bool:
    return any(re.search(p, text) for p in OUT_OF_SCOPE_PATTERNS)


def format_education_reply(r: EducationResult) -> str:
    lines = [r.answer]
    if r.needs_medical_attention and r.follow_up_hint:
        lines += ["", f"⚠️ {r.follow_up_hint}"]
    if r.sources:
        lines += ["", f"参考：{'；'.join(r.sources)}"]
    lines += ["", "以上为健康科普，不能替代医生诊断。"]
    return "\n".join(lines)


async def education_agent_node(state: ConsultState) -> Command:
    query = state["messages"][-1].content

    if check_out_of_scope(query):
        return Command(
            goto="supervisor",
            update={
                "messages": [AIMessage(content="这个问题涉及个人医疗建议，建议咨询医生，或让我帮您做分诊。")],
                "active_agent": None,
            },
        )

    docs = retriever.retrieve(query, k=5)
    context = format_retrieved_context(docs)

    result = await model.with_structured_output(EducationResult).ainvoke([
        SystemMessage(content=EDUCATION_PROMPT),
        SystemMessage(content=f"检索资料：\n{context}"),
        *state["messages"],
    ])

    if result.out_of_scope:
        return Command(
            goto="supervisor",
            update={
                "messages": [AIMessage(content="这个问题涉及个人医疗建议，建议咨询医生。")],
                "active_agent": None,
            },
        )

    return Command(
        goto="supervisor",
        update={"messages": [AIMessage(content=format_education_reply(result))], "active_agent": None},
    )