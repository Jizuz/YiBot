import asyncio
import re

from langchain_core.messages import AIMessage, SystemMessage
from langgraph.types import Command

from agent.struct.consult_state import ConsultState
from agent.struct.schemas import EducationResult, KBSearchPlan
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

KB_GROUP_ANALYSIS_PROMPT = """你是健康知识库检索的分组分析器。请根据用户请求（结合会话上下文）判断问题所属的健康知识分组，作为知识库检索的过滤条件。

分组取值（只能从中选择）：
- cardio_health：心血管（高血压、冠心病、心悸、心律失常、血脂异常等）
- resp_health：呼吸（感冒、咳嗽、咳痰、哮喘、肺炎、慢阻肺等）
- ped_health：儿童健康（婴幼儿/儿童/青少年特有的喂养、生长发育、儿童常见病等，问题主体是儿童且非成人通用疾病）
- endo_health：内分泌（糖尿病、甲状腺疾病、肥胖、痛风、骨质疏松等代谢内分泌问题）
- women_health：女性健康（孕产保健、月经、妇科问题、更年期等）
- common_living：通用居家健康（饮食养生、运动、睡眠、生活注意事项等不属于以上分组的一般健康问题）
- unknown：无法判断、跨分组或模棱两可

判断规则：
1. 以疾病/症状的核心医学领域为准，而非用户身份特征（如"孩子咳嗽"核心是呼吸系统问题→resp_health）；
2. 问题本身属于特定人群领域时取该人群分组（如儿童喂养发育→ped_health、孕产保健→women_health、糖尿病饮食→endo_health）；
3. 跨分组、信息不足或模棱两可时选 unknown（检索时不过滤分组，保证召回覆盖）；
4. 只做分组判断，不要回答用户的问题，不要臆测用户未提及的信息。
"""

# search_knowledge_base 的 docGroup 合法取值(与服务端枚举一致,入组仅限这些值,其余一律不传分组)
KB_DOC_GROUPS = {
    "cardio_health",  # 心血管
    "resp_health",    # 呼吸
    "ped_health",     # 儿童健康
    "endo_health",    # 内分泌
    "women_health",   # 女性健康
    "common_living",  # 通用居家健康
}

# 服务端"空结果"话术特征,用于识别分组过滤后无命中
KB_NO_RESULT_MARK = "未检索到相关内容"

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


def _kb_group_arg_name(tool) -> str | None:
    """推断 search_knowledge_base 的分组参数名: 优先 docGroup,否则取 schema 中名称含 group 的字符串字段,无则返回 None(不传分组)。"""
    fields = getattr(getattr(tool, "args_schema", None), "model_fields", None) or {}
    names = list(fields)
    if "docGroup" in names:
        return "docGroup"
    for name in names:
        if "group" in name.lower() and getattr(fields[name], "annotation", None) is str:
            return name
    return None


async def _analyze_kb_group(state: ConsultState) -> str | None:
    """
    大模型分析用户请求,得到 search_knowledge_base 的知识库分组(docGroup)。
    不确定/跨分组返回 None,分析异常也返回 None(检索时不过滤分组、全分组召回),不阻塞主流程。
    """
    try:
        plan = await model.with_structured_output(KBSearchPlan).ainvoke([
            SystemMessage(content=KB_GROUP_ANALYSIS_PROMPT),
            *state["messages"],
        ])
    except (Exception, asyncio.CancelledError):
        print("警告：知识库分组分析失败,检索时不过滤分组")
        return None
    doc_group = getattr(plan, "doc_group", None)
    if doc_group in (None, "", "unknown"):
        print(f"ℹ️ 知识库分组未明确判定({getattr(plan, 'reason', '') or '未给出理由'}),检索全部分组")
        return None
    print(f"ℹ️ 知识库分组分析: {doc_group}({plan.reason})")
    return doc_group


async def _search_kb_via_mcp(query: str, doc_group: str | None = None) -> str | None:
    """
    通过 MCP 可选工具 search_knowledge_base 检索权威科普片段,可附带 docGroup 分组过滤。
    分组过滤后无命中时自动退回全分组重检一次(防分组过窄漏检);
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

    group_arg = _kb_group_arg_name(tool)
    # 分组白名单校验: 入组仅限服务端枚举值,非法值一律不传分组
    if doc_group and doc_group not in KB_DOC_GROUPS:
        print(f"⚠️ 非法知识库分组 {doc_group}(入组仅限 {'/'.join(sorted(KB_DOC_GROUPS))}),已忽略分组过滤")
        doc_group = None

    async def _invoke(group: str | None) -> str | None:
        args = {_kb_query_arg_name(tool): query}
        if group and group_arg:
            args[group_arg] = group
        try:
            text = await asyncio.wait_for(tool.ainvoke(args), timeout=MCP_TIMEOUT)
        except (Exception, asyncio.CancelledError):
            print("警告：MCP search_knowledge_base 调用失败/超时")
            return None
        return str(text or "").strip()

    text = await _invoke(doc_group)
    if doc_group and (not text or KB_NO_RESULT_MARK in text):
        # 分组过滤后无命中: 退回全分组重检一次,保留原结果兜底
        print(f"ℹ️ 分组 {doc_group} 检索无结果,退回全部分组重检")
        text = await _invoke(None) or text
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
    # 分组过滤: 先由大模型分析用户请求所属健康分组作为 docGroup,不确定时不传分组检索全部。
    doc_group = await _analyze_kb_group(state)
    context = await _search_kb_via_mcp(query, doc_group=doc_group)
    if context:
        print(f"✅ education 检索命中 MCP search_knowledge_base(分组: {doc_group or '全部'})")
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