"""分层记忆工具: 滚动窗口 + 摘要折叠(Supervisor 全局记忆与子 Agent 私有记忆共用)。"""
from langchain_core.messages import HumanMessage, SystemMessage

# 主信箱(ConsultState.messages)保留的最近轮数, 1 轮 = Human + AI, 旧消息由 Supervisor 折叠进 global_summary
WINDOW_ROUNDS = 15
# 子 Agent 私有镜像消息条数上限, 超过则把头部折叠进私有摘要
PRIVATE_MSG_LIMIT = 12
# 折叠时私有镜像保留的尾部条数(保证覆盖"上一问 + 最新答"等槽位抽取依赖)
PRIVATE_KEEP_TAIL = 8

FOLD_PROMPT = """你是对话记忆管理器。请把【已有摘要】与【待归档对话】合并为一份新的摘要，供后续对话的调度与回复作背景参考。

要求：
- 保留全部事实性信息：用户姓名/手机号等身份信息、症状描述与采集进度、分诊结论、已查询到的权益及结果、预约办理进度（权益名、门店、日期、是否完成）、用户明确表达的偏好
- 保留时间线与当前进行中的事项
- 丢弃寒暄、重复与无关细节
- 用第三人称中文简洁陈述，不超过300字

【已有摘要】
{existing}

【待归档对话】
{conversation}"""


def recent_rounds(msgs: list, k_rounds: int = WINDOW_ROUNDS) -> list:
    """取最近 k_rounds 轮消息（保证以 Human 开头，轮次完整），供路由/收尾等轻量调用作输入。"""
    limit = k_rounds * 2 + 2
    tail = msgs[-limit:] if len(msgs) > limit else list(msgs)
    for i, m in enumerate(tail):
        if isinstance(m, HumanMessage):
            return tail[i:]
    return tail


def _fmt_msg(m) -> str:
    role = "用户" if isinstance(m, HumanMessage) else "助手"
    return f"{role}: {m.content}"


async def fold_messages(model, fold_part: list, existing_summary: str | None) -> str | None:
    """把 fold_part 归档进摘要，返回新摘要；空输入或失败返回 None 之外的降级由调用方处理。"""
    if not fold_part:
        return existing_summary
    conversation = "\n".join(_fmt_msg(m) for m in fold_part)
    try:
        resp = await model.ainvoke([
            SystemMessage(content=FOLD_PROMPT.format(
                existing=existing_summary or "（暂无）",
                conversation=conversation,
            )),
        ])
        return resp.content
    except Exception:
        return None


def background_block(summary: str | None, title: str = "会话背景摘要（此前对话的记忆）") -> str:
    """把摘要格式化为可拼进 System Prompt 的背景段；无摘要返回空串。"""
    return f"\n\n## {title}\n{summary}" if summary else ""
