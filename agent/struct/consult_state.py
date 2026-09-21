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
    # 分层记忆
    global_summary: str | None   # Supervisor 全局汇总记忆: 主信箱超出滚动窗口的旧消息折叠而来(仅 supervisor 维护)
    rights_memory: dict | None   # rights_agent 私有记忆(仅 rights_agent 读写, supervisor 只读 appointing 做硬路由):
                                 # {summary: 历史折叠摘要, messages: 对话镜像, answered: 守卫标记, appointing: 预约槽位状态机}