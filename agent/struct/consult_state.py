from typing import Annotated, TypedDict
from langgraph.graph import add_messages

class ConsultState(TypedDict):
    messages: Annotated[list, add_messages]
    user_id: int
    session_id: str              # 当前会话 ID(入口传入, 供子 Agent 使用)
    next_agent: str | None        # Supervisor 的路由决策
    need_emergency: bool          # 红旗标记
    summary: str | None           # 诊前摘要
    # 子 Agent 多轮相关
    active_agent: str | None      # 当前活跃的子 Agent
    agent_turn_count: int
    pending_triage: bool          # 症状采集完成后置 True
    symptom_summary: str | None   # 采集到的症状总结，供分诊使用
    rights_answered: bool         # rights_agent 已给出回复(仅 rights_agent 读写, 用于拦截 supervisor 无新输入的重复路由)
    rights_booking: dict | None   # 权益预约流程槽位状态(仅 rights_agent 读写): stage/name/mobile/datetime/desired_right/location/chosen_store/right_id