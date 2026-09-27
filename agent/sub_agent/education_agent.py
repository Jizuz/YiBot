import asyncio
import re

from langchain_core.messages import AIMessage, SystemMessage
from langgraph.types import Command

from agent.struct.consult_state import ConsultState
from agent.struct.schemas import EducationResult
from llm.base_llm import chat_model
from rag.core.hybird import format_retrieved_context
from rag.core.hybird import HybridRetriever as retriever
from tools.mcp import MCP_TIMEOUT, TOOL_SEARCH_KNOWLEDGE_BASE, get_mcp_tool_by_name

model = chat_model()

EDUCATION_PROMPT = """你是健康科普助手，用通俗语言回答健康知识问题。

能回答：疾病知识、症状的科普性解读、检查指标一般意义、饮食养生、就医建议等健康科普问题。
不能回答：个人诊断、具体用药、解读个人报告下结论。

回答依据（检索资料来自健康科普知识库工具 search_knowledge_base 或本地知识库，均为权威科普片段）：
1. 仅基于检索资料回答，严禁编造知识库中没有的医学内容；
2. 检索资料未覆盖该问题时，如实说明暂无可靠科普资料，并建议咨询医生，不要臆测；
3. 涉及个人情况提示咨询医生；超出健康科普范围设 out_of_scope=true。
"""

OUT_OF_SCOPE_PATTERNS = [
    r"我(这|该|要)吃(什么|啥)药",
    r"帮我(看看|解读).*(报告|检查|化验)",
    r"我(是不是|得了|这是).*(病|癌|炎)",
    r"(给我|帮我).*(开药|处方|剂量)",
]


def check_out_of_scope(text: str) -> bool:
    return any(re.search(p, text) for p in OUT_OF_SCOPE_PATTERNS)


def _kb_query_arg_name(tool) -> str:
    """推断 search_knowledge_base 的查询参数名: 优先 query,否则取服务端 schema 中第一个字符串字段,兜底 query。"""
    fields = getattr(getattr(tool, "args_schema", None), "model_fields", None) or {}
    names = list(fields)
    if not names or "query" in names:
        return "query"
    for name in names:
        if getattr(fields[name], "annotation", None) is str:
            return name
    return names[0]


async def _search_kb_via_mcp(query: str) -> str | None:
    """
    通过 MCP 可选工具 search_knowledge_base 检索权威科普片段。
    工具未暴露 / 连接失败 / 调用超时一律返回 None,由调用方降级本地 RAG,不阻塞主流程。
    (CancelledError 为 BaseException,需显式捕获,同 rights_agent 的 _get_tools_safe)
    """
    try:
        tool = await asyncio.wait_for(get_mcp_tool_by_name(TOOL_SEARCH_KNOWLEDGE_BASE), timeout=15.0)
    except (Exception, asyncio.CancelledError):
        print("警告：MCP search_knowledge_base 获取失败/超时")
        return None
    if tool is None:
        return None
    try:
        text = await asyncio.wait_for(
            tool.ainvoke({_kb_query_arg_name(tool): query}), timeout=MCP_TIMEOUT
        )
    except (Exception, asyncio.CancelledError):
        print("警告：MCP search_knowledge_base 调用失败/超时")
        return None
    text = str(text or "").strip()
    return text or None


# ==================== 科普无法满足时的转接(症状采集 -> 分诊链路) ====================
# 触发条件: 检索资料未覆盖该问题 / 涉及个人情况 / 超出健康科普范围。
# 实现: 引导语写入主信箱 + pending_symptom_collect=True 回 supervisor,
#       由 supervisor 硬路由转 symptom_agent, 采集完成后经 pending_triage 硬路由进 triage_agent。

HANDOFF_OOS_MSG = (
    "这个问题涉及您的个人医疗情况，仅靠健康科普无法给出可靠建议。"
    "我先了解一下您的具体症状，再帮您分诊到合适的科室。"
)

HANDOFF_NO_COVER_MSG = (
    "知识库中暂无这个问题的可靠科普资料，为避免误导就不凭空回答啦。"
    "我先收集一些症状细节，帮您做分诊建议。"
)

HANDOFF_NEED_ATTENTION_SUFFIX = (
    "\n\n⚠️ 您的情况建议进一步就医确认。接下来我收集一些症状细节，帮您判断合适的就诊科室。"
)


def _handoff_to_symptom(msg: str) -> Command:
    """科普无法满足时的统一转接出口: 引导语回 supervisor, 由硬路由转症状采集->分诊链路。"""
    return Command(
        goto="supervisor",
        update={
            "messages": [AIMessage(content=msg)],
            "active_agent": None,
            "pending_symptom_collect": True,
        },
    )


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

    # 越界(正则前置): 不检索不生成, 直接转症状采集->分诊链路
    if check_out_of_scope(query):
        return _handoff_to_symptom(HANDOFF_OOS_MSG)

    # 检索通道: MCP 权威知识库(search_knowledge_base,可选)优先,不可用时降级本地 RAG 混合检索。
    # 仅健康科普问题会走到这里(越界请求已被上方正则前置拦截),符合工具"仅用于健康科普助手场景"的边界。
    context = await _search_kb_via_mcp(query)
    if context:
        print("✅ education 检索命中 MCP search_knowledge_base")
    else:
        print("ℹ️ education 使用本地 RAG 检索(MCP search_knowledge_base 不可用)")
        docs = retriever.retrieve(query, k=5)
        if not docs:
            # 检索资料未覆盖: 不让 LLM 凭空生成, 转症状采集->分诊链路
            print("ℹ️ education 检索未覆盖该问题, 转症状采集->分诊链路")
            return _handoff_to_symptom(HANDOFF_NO_COVER_MSG)
        context = format_retrieved_context(docs)

    result = await model.with_structured_output(EducationResult).ainvoke([
        SystemMessage(content=EDUCATION_PROMPT),
        SystemMessage(content=f"检索资料：\n{context}"),
        *state["messages"],
    ])

    # 越界(LLM 第二层,防正则漏网): 同样转症状采集->分诊链路
    if result.out_of_scope:
        return _handoff_to_symptom(HANDOFF_OOS_MSG)

    # 涉及个人情况: 先给出科普回答, 再转症状采集->分诊链路
    if result.needs_medical_attention:
        return _handoff_to_symptom(format_education_reply(result) + HANDOFF_NEED_ATTENTION_SUFFIX)

    return Command(
        goto="supervisor",
        update={"messages": [AIMessage(content=format_education_reply(result))], "active_agent": None},
    )