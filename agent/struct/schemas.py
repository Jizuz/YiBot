from pydantic import BaseModel, Field
from typing import Literal

class SupervisorDecision(BaseModel):
    """Supervisor 的结构化路由决策。"""
    route: Literal["triage_agent", "symptom_agent", "education_agent", "FINISH"] = Field(description="下一个要调用的子 Agent，或 FINISH 表示结束")
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