from pydantic import BaseModel, Field
from typing import Literal

class SupervisorDecision(BaseModel):
    """Supervisor 的结构化路由决策。"""
    route: Literal["triage_agent", "symptom_agent", "education_agent", "rights_agent", "FINISH"] = Field(description="下一个要调用的子 Agent，或 FINISH 表示结束")
    reason: str = Field(description="路由理由，用于日志和调试")


class CollectionStatus(BaseModel):
    """症状采集进度判断结果。"""
    
    decision: Literal["continue", "summarize"] = Field(
        description=(
            "continue：还需要继续追问；"
            "summarize：关键维度已收集齐，可以总结"
        )
    )
    
    collected: dict[str, str | None] = Field(
        description="已收集到的维度及其值，未收集的维度值为 null",
        default_factory=dict,
    )
    
    missing: list[str] = Field(
        description="仍然缺失的关键维度列表",
        default_factory=list,
    )
    
    next_question: str | None = Field(
        description="如果 decision 是 continue，给出下一个要问的问题；否则为 null",
        default=None,
    )
    
    reason: str = Field(description="判断理由，用于调试")


class TriageResult(BaseModel):
    """分诊结果。"""
    
    urgency: Literal["P0", "P1", "P2", "P3"] = Field(
        description=(
            "P0：危及生命，需立即急救；"
            "P1：可能严重，需 24 小时内就医；"
            "P2：需要就诊，近期门诊即可；"
            "P3：可居家观察或仅需科普"
        )
    )
    
    department: str = Field(
        description="建议就诊科室，如'消化内科'、'神经内科'、'急诊科'"
    )
    
    alternative_departments: list[str] = Field(
        description="备选科室，当主科室不确定时提供",
        default_factory=list,
    )
    
    action_advice: str = Field(
        description="用户当前应采取的行动，一句话，口语化"
    )
    
    reasoning: str = Field(
        description="分诊理由，简述判断依据"
    )
    
    red_flags: list[str] = Field(
        description="识别到的红旗症状列表，没有则为空",
        default_factory=list,
    )


class RightsIntent(BaseModel):
    """rights_agent 意图分流结果。"""

    intent: Literal["query", "booking"] = Field(
        description=(
            "query：查询/了解类诉求（查我有什么权益、看有效期、问门店等）；"
            "booking：使用/办理类诉求（我想用某项权益、帮我预约等）"
        )
    )


class BookingTurn(BaseModel):
    """权益预约流程：每轮从用户最新消息中抽取槽位与意图。"""

    intent: Literal["cancel", "continue", "other"] = Field(
        description=(
            "cancel：用户明确表达不想预约了、取消、算了等退出意愿；"
            "other：用户消息与权益预约完全无关（如查询权益、闲聊）；"
            "continue：用户在继续预约流程（补充信息、回答问题、选择门店等）"
        )
    )

    user_name: str | None = Field(default=None, description="用户真实姓名，仅取用户明确给出的内容")
    mobile: str | None = Field(default=None, description="11 位手机号")
    appoint_datetime: str | None = Field(default=None, description="预约日期，标准化为 YYYY-MM-DD")
    desired_right: str | None = Field(default=None, description="用户想使用的权益名称，如：洁牙一次卡")
    location: str | None = Field(default=None, description="用户所在城市或区域")
    chosen_store: str | None = Field(default=None, description="用户选择/提到的门店名称")
    next_question: str | None = Field(
        default=None,
        description="intent=continue 且仍有必填信息缺失时，生成自然的下一个追问（一次只问一项）；否则为 null",
    )


class RightMatch(BaseModel):
    """从用户权益列表中匹配意向权益的结果。"""

    right_id: int | None = Field(
        default=None,
        description="匹配到的权益 rightId；无法唯一确定时为 null",
    )


class EducationResult(BaseModel):
    """科普回答结果。"""
    
    answer: str = Field(
        description="科普正文，通俗易懂，面向普通用户"
    )
    
    sources: list[str] = Field(
        description="知识来源，如'《中国2型糖尿病防治指南(2020版)》'",
        default_factory=list,
    )
    
    needs_medical_attention: bool = Field(
        description="回答中是否涉及需要就医的情况"
    )
    
    follow_up_hint: str | None = Field(
        description="如果涉及个人情况，给出就医/咨询医生的提示",
        default=None,
    )
    
    out_of_scope: bool = Field(
        description="问题是否超出科普范围（如要求诊断、开药）",
        default=False,
    )