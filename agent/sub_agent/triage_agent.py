from langchain_core.messages import SystemMessage, AIMessage
from langgraph.types import Command
from agent.struct.consult_state import ConsultState
from agent.struct.schemas import TriageResult
from llm.base_llm import chat_model

model = chat_model()

TRIAGE_PROMPT = """你是一个医疗分诊助手。你的任务是根据用户描述的症状，评估紧急程度并推荐就诊科室。

## 紧急程度分级标准

- **P0（立即急救）**：出现以下任一情况
  - 胸痛伴大汗/呼吸困难
  - 突发剧烈头痛（"一生中最痛"）
  - 意识障碍、昏迷、抽搐
  - 大出血、呕血、便血
  - 严重呼吸困难、口唇发紫
  - 疑似中风（口角歪斜、单侧肢体无力、言语不清）

- **P1（24小时内就医）**：症状较重但不即刻危及生命
  - 持续高热超过 3 天
  - 剧烈腹痛、持续呕吐
  - 不明原因消瘦、持续咯血
  - 新发严重症状且进行性加重

- **P2（近期门诊）**：需要就诊但非紧急
  - 一般性疼痛、发热、咳嗽
  - 慢性症状加重
  - 皮疹、消化不良等

- **P3（居家观察）**：轻微、自限性问题
  - 轻微感冒、偶发头痛
  - 明确诱因的轻微不适

## 科室推荐原则

- 根据**主要症状**推荐最对口的科室
- 不确定时给备选科室
- 紧急情况统一推荐"急诊科"

## 输出要求

- urgency：严格按上述标准判断
- department：主科室
- alternative_departments：备选（可为空）
- action_advice：一句话，告诉用户现在该做什么
- reasoning：简述判断依据
- red_flags：识别到的红旗症状

## 重要约束

- 不要确诊疾病，只做分诊
- 不要推荐具体药物或剂量
- 如果信息不足以判断，按更保守（更紧急）的级别处理
- 每次分诊必须给出 action_advice
"""

async def triage_agent_node(state: ConsultState) -> Command:
    """分诊 Agent：评估紧急程度 + 推荐科室。"""
    last_user_msg = state["messages"][-1].content
    
    # 前置校验
    forced_level = pre_triage_check(last_user_msg)
    if forced_level == "P0":
        return Command(
            goto="emergency_response",
            update={
                "messages": [AIMessage(content="⚠️ 危及生命，需立即急救！！！")],
                "need_emergency": True,
                "active_agent": None,
            },
        )

    # 如果有采集好的症状总结，拼进上下文，避免重复采集
    extra_context = ""
    if state.get("symptom_summary"):
        extra_context = f"\n\n【已采集的症状信息】\n{state['symptom_summary']}"
    
    result = await model.with_structured_output(TriageResult).ainvoke([
        SystemMessage(content=TRIAGE_PROMPT + extra_context),
        *state["messages"],
    ])

    # 规则层与 LLM 结果取更紧急的
    if forced_level == "P1" and result.urgency in ("P2", "P3"):
        result.urgency = "P1"
        result.reasoning += "（规则层升级：检测到高风险关键词）"
    
    # P0 强制走紧急通道，不再返回 Supervisor 做常规处理
    if result.urgency == "P0":
        return Command(
            goto="emergency_response",
            update={
                "messages": [AIMessage(content=format_triage_reply(result))],
                "need_emergency": True,
                "active_agent": None,
            },
        )
    
    # P1-P3 正常返回 Supervisor
    return Command(
        goto="supervisor",
        update={
            "messages": [AIMessage(content=format_triage_reply(result))],
            "active_agent": None,
        },
    )

#==================== 结构化输出 ====================
def format_triage_reply(r: TriageResult) -> str:
    """把结构化分诊结果格式化成用户可读的回复。"""
    urgency_label = {
        "P0": "🚨 紧急",
        "P1": "⚠️ 尽快就医",
        "P2": "建议就诊",
        "P3": "可先观察",
    }[r.urgency]
    
    lines = [f"【分诊结果】{urgency_label}"]
    lines.append(f"建议科室：{r.department}")
    if r.alternative_departments:
        lines.append(f"备选科室：{'、'.join(r.alternative_departments)}")
    lines.append(f"行动建议：{r.action_advice}")
    lines.append("")
    lines.append("以上为分诊参考，不能替代医生诊断。")
    return "\n".join(lines)

#==================== 规则前置校验 ====================
P0_KEYWORDS = [
    "胸痛", "呼吸困难", "昏迷", "抽搐", "大出血",
    "呕血", "便血", "口角歪斜", "半身不遂", "说不出话",
    "剧烈头痛", "意识不清", "口唇发紫",
]

P1_KEYWORDS = ["高热不退", "持续呕吐", "咯血", "剧烈腹痛", "消瘦"]

def pre_triage_check(text: str) -> str | None:
    """规则层前置筛查，命中直接返回紧急级别。"""
    for kw in P0_KEYWORDS:
        if kw in text:
            return "P0"
    for kw in P1_KEYWORDS:
        if kw in text:
            return "P1"
    return None