import asyncio
import json
from datetime import date

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.types import Command

from agent.struct.consult_state import ConsultState
from agent.struct.memory import (
    PRIVATE_KEEP_TAIL,
    PRIVATE_MSG_LIMIT,
    background_block,
    fold_messages,
)
from agent.struct.schemas import AppointingTurn, RightMatch, RightsIntent
from llm.base_llm import chat_model
from tools.mcp import get_mcp_tools

model = chat_model()

RIGHTS_PROMPT = """你是用户权益服务助手，负责帮助用户查询和使用医疗健康类权益（如洁牙卡、体检卡等）。

## 可用工具（由 MCP Server 提供，以实际加载为准）
- get_right_list： 查询用户权益列表，参数 userId（整数）
- get_nearby_stores： 查询附近可用门店，参数 location（地点描述）
- queryWeather： 查询天气，参数 city（城市）

## 工作规则
1. 用户查询权益、询问自己有什么权益时：调用 get_right_list，用简洁列表呈现权益名称、有效期等关键信息
2. 用户想使用权益或寻找服务门店时：先调用 get_right_list 确认权益，再根据用户所在位置调用 get_nearby_stores 推荐门店
3. 严格基于工具返回结果回答，禁止编造权益或门店数据
4. 工具调用失败时，如实告知用户权益服务暂时不可用，请稍后再试
5. 用中文简洁、友好地回答
6. 用户提出"使用权益、预约服务"等办理类诉求时，不要调用工具，说明将转由专属预约流程为其办理

{user_id_hint}"""


# ==================== 权益预约流程 ====================
# 跨请求多轮状态机:
#   collect(姓名/手机号/日期/意向权益) -> 
#   get_right_list 匹配 rightId -> 
#   valid_right 校验 -> 
#   wait_location -> 
#   get_nearby_stores -> 
#   wait_store(用户选店) -> 
#   appoint_service 下单
# 任一环节失败/用户取消: 委婉终止并清空状态, 本次不预约。

RIGHTS_INTENT_PROMPT = """判断用户当前对"医疗健康权益"的诉求类型，只输出分类：
- query：查询/了解类（查我有什么权益、看有效期、问哪家门店可以用等）
- appointing：使用/办理类（我想用某项权益、帮我预约洁牙、我要使用体检卡等）"""

APPOINTING_TURN_PROMPT = """你是医疗健康权益的预约助手，正在通过多轮对话为用户办理权益使用预约。

今天是 {today}。

当前已收集的预约信息：
{slots}

当前流程阶段：{stage}

请仔细阅读对话历史（重点理解用户最新一条消息），完成：
1. 判断 intent（cancel=用户要取消/算了；other=与权益预约完全无关；continue=继续预约流程）
2. 抽取槽位（仅基于用户明确给出的内容，禁止推测编造，没有则 null）：
   user_name 真实姓名 / mobile 11位手机号 / appoint_datetime 预约日期(标准化为 YYYY-MM-DD，口语日期按今天换算)
   / desired_right 想使用的权益名称（若用户用序号回答如"第一个/1"，请结合上一轮列出的权益换成对应权益名称）
   / location 所在城市或区域 / chosen_store 选择的门店名称（若用户用序号回答，结合上一轮门店列表换成门店名称）
   ⚠ appoint_datetime 必须来自用户明确说过的日期；用户没有提到任何日期时必须为 null，严禁用今天或其他日期默认填充
3. next_question：intent=continue 且仍有必填信息缺失时，生成自然友好的下一个追问（一次只问一项）；否则 null"""

RIGHT_MATCH_PROMPT = """你是权益匹配助手。

用户此前表达的意向权益：{desired}
用户最新回复（可能是权益名称、序号如"第一个/1/第二项"、或口语指代）：{last_reply}

用户名下的权益列表（按展示顺序）：
{rights}

请判断用户想使用哪一项权益（返回该条目中 rightId 字段的整数值）：
- 用户最新回复中的名称或序号能唯一对应列表中某一项时，返回该项的 rightId（如"第一个"对应列表第 1 项）
- 最新回复无法判断但此前意向明确唯一对应某项时，返回该项的 rightId
- 无法唯一确定时，rightId 返回 null"""

APPOINTING_SERVICE_DOWN_MSG = "抱歉，权益预约服务暂时不太稳定，为避免出错本次就先不为您预约啦～您可以稍后再试，或联系客服协助处理，感谢理解！"
APPOINTING_CANCEL_MSG = "好的，已为您取消本次预约申请～您随时想预约都可以再找我，祝您生活愉快！"
APPOINTING_VALID_FAIL_MSG = "很抱歉，您选择的权益暂未通过核验（可能已过期或不满足使用条件），本次就先不为您安排预约啦～您可以看看名下其他权益，或联系客服核实，感谢理解！"

_SLOT_LABELS = [
    ("user_name", "姓名"), 
    ("mobile", "手机号"), 
    ("appoint_datetime", "预约日期"),
    ("desired_right", "意向权益"), 
    ("location", "所在区域"), 
    ("chosen_store", "选定门店"),
]


def _appointing_slots_desc(appointing: dict) -> str:
    return "；".join(f"{lbl}：{appointing.get(k) or '待收集'}" for k, lbl in _SLOT_LABELS)


def _format_entity_list(text: str, name_key: str, skip_keys: tuple = ()) -> str:
    """从工具返回文本中提取 JSON 数组并格式化为可读列表；无 JSON 或解析失败时原样返回。"""
    start = text.find("[")
    if start < 0:
        return text
    try:
        arr = json.loads(text[start:])
    except (ValueError, TypeError):
        return text
    if not isinstance(arr, list) or not arr:
        return text
    lines = []
    for i, item in enumerate(arr, 1):
        if not isinstance(item, dict):
            lines.append(f"{i}. {item}")
            continue
        name = str(item.get(name_key, "")).strip()
        extras = [str(v) for k, v in item.items() if k not in (name_key, *skip_keys) and v]
        lines.append(f"{i}. {name}（{'，'.join(extras)}）" if extras else f"{i}. {name}")
    return "\n".join(lines)


def _merge_slots(appointing: dict, turn) -> dict:
    for key, _ in _SLOT_LABELS:
        val = getattr(turn, key, None)
        # GLM structured output 偶尔会把"未提供"输出为字面字符串 "null"/"none",
        # 一律视为未提供, 防止脏值覆盖已收集槽位/传入 MCP 下单参数
        if val and str(val).strip().lower() not in ("null", "none"):
            appointing[key] = val
    return appointing


# ==================== rights_agent 私有记忆 ====================

def _init_mem(state: ConsultState) -> dict:
    """初始化私有记忆结构: {summary, messages, answered, appointing}。"""
    mem = state.get("rights_memory")
    if isinstance(mem, dict):
        mem.setdefault("summary", None)
        mem.setdefault("messages", [])
        mem.setdefault("answered", False)
        mem.setdefault("appointing", None)
        return mem
    return {"summary": None, "messages": [], "answered": False, "appointing": None}


def _sync_mirror(mem: dict, state: ConsultState) -> dict:
    """把主agent尾部新出现的用户消息(最后一条 AI 之后)补进私有镜像，幂等。"""
    msgs = state["messages"]
    idx = len(msgs)
    while idx > 0 and isinstance(msgs[idx - 1], HumanMessage):
        idx -= 1
    fresh = list(msgs[idx:])
    if fresh:
        existing = mem.get("messages") or []
        if not existing or getattr(existing[-1], "id", None) != getattr(fresh[-1], "id", None):
            mem["messages"] = existing + fresh
    return mem


async def _fold_mirror(mem: dict) -> None:
    """私有镜像超限时把头部折叠进 mem.summary(摘要失败降级丢弃头部，保住最新问答)。"""
    msgs = mem.get("messages") or []
    if len(msgs) <= PRIVATE_MSG_LIMIT:
        return
    head, tail = msgs[:-PRIVATE_KEEP_TAIL], msgs[-PRIVATE_KEEP_TAIL:]
    new_summary = await fold_messages(model, head, mem.get("summary"))
    if new_summary is None:
        print("警告：rights 私有记忆摘要失败，降级直接丢弃较旧消息")
    else:
        mem["summary"] = new_summary
    mem["messages"] = tail


def _rights_emit(mem: dict, ai_msg: AIMessage, goto: str, extra: dict | None = None) -> Command:
    """rights_agent 统一出口：回复双写(主agent + 私有镜像)，并整体持久化 rights_memory。"""
    (mem.setdefault("messages", [])).append(ai_msg)
    update = {"messages": [ai_msg], "rights_memory": mem}
    if extra:
        update.update(extra)
    return Command(goto=goto, update=update)


def _appointing_ask(mem: dict, question: str) -> Command:
    """追问并结束本轮，等待用户下一条消息（跨请求多轮，槽位随 rights_memory 持久化）。"""
    return _rights_emit(mem, AIMessage(content=question), "__end__")


def _appointing_finish(mem: dict, msg: str) -> Command:
    """预约流程终态：回复用户并清空流程状态，返回 supervisor 收尾。"""
    mem["appointing"] = None
    mem["answered"] = True
    return _rights_emit(mem, AIMessage(content=msg), "supervisor", {"active_agent": None})


async def _get_tools_safe() -> list:
    """获取 MCP 工具列表，失败/挂起一律降级为空列表（CancelledError 为 BaseException，需显式捕获）。"""
    try:
        tools = await asyncio.wait_for(get_mcp_tools(), timeout=15.0)
    except (Exception, asyncio.CancelledError):
        print("警告：MCP 连接失败/超时")
        tools = []
    return tools if isinstance(tools, list) else []


async def _call_mcp(tools: list, name: str, arguments: dict) -> str | None:
    """按名调用 MCP 工具，失败/超时返回 None（不抛异常，由调用方降级）。"""
    tool = next((t for t in tools if getattr(t, "name", None) == name), None)
    if tool is None:
        return None
    try:
        return await asyncio.wait_for(tool.ainvoke(arguments), timeout=15.0)
    except (Exception, asyncio.CancelledError):
        return None


def _valid_passed(text: str | None) -> bool:
    """按返回文本判定权益校验是否通过；无法明确判定时保守视为不通过（宁可少约，不可误约）。"""
    if not text:
        return False
    if any(w in text for w in ("失败", "不可用", "过期", "已使用")):
        return False
    return any(w in text for w in ("通过", "可以使用", "可用"))


def _appoint_outcome(text: str | None) -> str:
    """判定预约下单结果：success / fail / unknown。"""
    if not text:
        return "fail"
    if any(w in text for w in ("失败", "不可", "错误")):
        return "fail"
    if any(w in text for w in ("成功", "已预约", "已为您预约")):
        return "success"
    return "unknown"


async def _appointing_extract(mem: dict, appointing: dict) -> AppointingTurn:
    """每轮抽取：槽位 + 意图（取消/继续/无关）。输入为私有记忆镜像尾部（含最新用户回复）。"""
    stage_desc = {
        "collect": "收集预约必填信息（姓名、手机号、预约日期、意向权益）",
        "wait_location": "权益与时间已核验，等待用户提供所在区域以推荐门店",
        "wait_store": "已推荐门店，等待用户选择预约哪家门店",
    }.get(appointing.get("stage"), "collect")
    return await model.with_structured_output(AppointingTurn).ainvoke([
        SystemMessage(content=APPOINTING_TURN_PROMPT.format(
            today=date.today().isoformat(),
            slots=_appointing_slots_desc(appointing),
            stage=stage_desc,
        )),
        *(mem.get("messages") or [])[-8:],
    ])


async def _appointing_flow(state: ConsultState, mem: dict, appointing: dict) -> Command | None:
    """预约流程状态机入口。返回 None 表示本轮与预约无关，回落到查询流程。"""
    turn = await _appointing_extract(mem, appointing)

    if turn.intent == "cancel":
        print("=== rights Agent 预约流程：用户取消 ===")
        return _appointing_finish(mem, APPOINTING_CANCEL_MSG)
    if turn.intent == "other":
        print("=== rights Agent 预约流程：本轮与预约无关，回落查询流程 ===")
        return None

    appointing = _merge_slots(dict(appointing), turn)
    mem["appointing"] = appointing  # 拷贝合并后写回私有记忆
    stage = appointing.get("stage", "collect")
    if stage == "wait_store":
        return await _appointing_choose_store(mem, appointing)
    if stage == "wait_location":
        return await _appointing_with_location(mem, appointing)
    return await _appointing_collect(state, mem, appointing, turn)


async def _appointing_collect(state: ConsultState, mem: dict, appointing: dict, turn) -> Command:
    """收集必填信息；齐备后查权益列表定 rightId，再调 valid_right 校验。"""
    missing = [lbl for key, lbl in _SLOT_LABELS[:3] if not appointing.get(key)]
    if missing:
        question = turn.next_question or f"请问您的{missing[0]}是？方便为您办理预约～"
        return _appointing_ask(mem, question)

    tools = await _get_tools_safe()
    if not tools:
        return _appointing_finish(mem, APPOINTING_SERVICE_DOWN_MSG)

    user_id = state.get("user_id")
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        user_id = None
    if not user_id:
        return _appointing_finish(mem, "抱歉，办理预约需要先确认您的会员身份，当前未能获取您的账号信息，本次就先不为您预约啦，您可以重新登录后再试～")

    rights_text = await _call_mcp(tools, "get_right_list", {"userId": user_id})
    # 返回无权益条目特征(如 mock Server 仅回状态文本)时, 视为未查到, 委婉终止
    if not rights_text or ("rightId" not in rights_text and "{" not in rights_text and "[" not in rights_text):
        return _appointing_finish(mem, "很抱歉，暂时没能查到您的权益信息，本次先不为您预约啦，请稍后再试～")
    print(f"=== rights Agent 预约流程：权益列表返回: {rights_text[:200]} ===")

    last_reply = next(
        (m.content for m in reversed(mem.get("messages") or []) if getattr(m, "type", "") == "human"),
        "",
    )
    match = await model.with_structured_output(RightMatch).ainvoke([
        SystemMessage(content=RIGHT_MATCH_PROMPT.format(
            desired=appointing.get("desired_right") or "（用户未明确指定）",
            last_reply=str(last_reply)[:200],
            rights=rights_text,
        )),
    ])
    right_id = getattr(match, "right_id", None)
    if not right_id:
        # 连续两次仍无法确定意向权益: 委婉终止, 避免反复重试同一问题
        fails = appointing.get("match_fails", 0) + 1
        if fails >= 2:
            print("=== rights Agent 预约流程：多次无法确定意向权益，委婉终止 ===")
            return _appointing_finish(mem, "抱歉，几次都没能确定您想使用哪项权益，本次就先不为您预约啦～您可以先查一下自己名下的权益名称，再来找我办理，感谢理解！")
        appointing["match_fails"] = fails
        # 无法唯一确定：列出权益请用户选择（回复序号或名称均可）
        rights_disp = _format_entity_list(rights_text, "rightName", skip_keys=("userId",))
        return _appointing_ask(mem, f"为您查到以下权益，请问您想使用哪一项呢？（直接回复序号或权益名称即可）\n{rights_disp}")
    appointing.pop("match_fails", None)
    appointing["right_id"] = right_id
    print(f"=== rights Agent 预约流程：匹配权益 rightId={right_id} ===")

    valid_text = await _call_mcp(tools, "valid_right", {"rightId": right_id})
    print(f"=== rights Agent 预约流程：valid_right 返回: {valid_text} ===")
    if not _valid_passed(valid_text):
        return _appointing_finish(mem, APPOINTING_VALID_FAIL_MSG)

    if appointing.get("location"):
        return await _appointing_with_location(mem, appointing, tools=tools)
    appointing["stage"] = "wait_location"
    return _appointing_ask(mem, "权益核验通过啦～请问您目前在哪个城市或区域呢？方便为您推荐附近可预约的门店。")


async def _appointing_with_location(mem: dict, appointing: dict, tools: list | None = None) -> Command:
    """有位置后查询门店列表，请用户选择。"""
    if not appointing.get("location"):
        appointing["stage"] = "wait_location"
        return _appointing_ask(mem, "请问您目前在哪个城市或区域呢？方便为您推荐附近可预约的门店。")
    if tools is None:
        tools = await _get_tools_safe()
    stores_text = await _call_mcp(tools, "get_nearby_stores", {"location": appointing["location"]}) if tools else None
    if not stores_text:
        return _appointing_finish(mem, APPOINTING_SERVICE_DOWN_MSG)
    appointing["stage"] = "wait_store"
    stores_disp = _format_entity_list(stores_text, "storeName")
    return _appointing_ask(mem, f"为您找到附近的可预约门店：\n{stores_disp}\n请问您想预约哪一家呢？")


async def _appointing_choose_store(mem: dict, appointing: dict) -> Command:
    """用户已选门店，调用 appoint_service 下单。"""
    if not appointing.get("chosen_store"):
        return _appointing_ask(mem, "请问您想预约哪一家门店呢？")
    if not appointing.get("right_id"):
        appointing["stage"] = "collect"
        return _appointing_ask(mem, "预约信息好像有点缺失，请再告诉我您想使用哪项权益，我马上为您安排～")

    tools = await _get_tools_safe()
    if not tools:
        return _appointing_finish(mem, APPOINTING_SERVICE_DOWN_MSG)

    result_text = await _call_mcp(tools, "appoint_service", {"req": {
        "appointDatetime": appointing.get("appoint_datetime"),
        "mobile": appointing.get("mobile"),
        "rightId": appointing.get("right_id"),
        "userName": appointing.get("user_name"),
    }})
    print(f"=== rights Agent 预约流程：appoint_service 返回: {result_text} ===")

    outcome = _appoint_outcome(result_text)
    if outcome == "fail":
        return _appointing_finish(mem, "很抱歉，刚才的预约没有办理成功，为避免出错本次就先不为您预约啦～您可以稍后再试，或联系客服协助处理，感谢理解！")
    if outcome == "success":
        return _appointing_finish(
            mem,
            f"预约办理成功啦～已为您预约「{appointing.get('chosen_store')}」，"
            f"日期 {appointing.get('appoint_datetime')}，请保持手机畅通以接收确认通知，祝您就诊顺利！"
        )
    return _appointing_finish(mem, "您的预约申请已提交，结果请以收到的短信/通知为准。如长时间未收到确认，可联系客服核实～")


async def rights_agent_node(state: ConsultState) -> Command:
    """权益 Agent：基于 MCP 工具处理权益查询 / 使用 / 附近门店等诉求。"""
    last_msg = state["messages"][-1].content
    print(f"=== rights Agent start, question: {last_msg} ===")

    # 私有记忆维护: 补进本轮新用户消息(主信箱尾部) -> 折叠超限头部
    mem = _sync_mirror(_init_mem(state), state)
    await _fold_mirror(mem)

    # 死循环守卫：本 Agent 刚回复完、其后没有新的用户输入，supervisor 却再次路由到本 Agent。
    # 该模式的唯一解释是 supervisor 路由循环(langgraph 1.x 默认 recursion_limit=10007, 会空转数千轮直到崩图)。
    # 已生成的回复保留在 messages 中, 直接交 final_response 收尾, 不再连接 MCP / 调用 LLM。
    msgs = state["messages"]
    if (
        mem.get("answered")
        and msgs
        and isinstance(msgs[-1], AIMessage)
        and not getattr(msgs[-1], "tool_calls", None)
    ):
        print("=== rights Agent 守卫触发: 无新用户输入的重复路由, 跳过查询直接收尾 ===")
        return Command(
            goto="final_response",
            update={"active_agent": None, "rights_memory": mem},
        )

    # 预约流程进行中: 优先走预约状态机(本轮与预约无关时自动回落查询流程)
    appointing = mem.get("appointing")
    if appointing:
        result = await _appointing_flow(state, mem, appointing)
        if result is not None:
            return result

    # 意图分流: 查询类 / 使用(预约)类 —— 输入用私有镜像尾部 + 全局背景摘要
    intent = await model.with_structured_output(RightsIntent).ainvoke([
        SystemMessage(content=RIGHTS_INTENT_PROMPT + background_block(state.get("global_summary"))),
        *(mem.get("messages") or [])[-6:],
    ])
    if intent and intent.intent == "appointing":
        print("=== rights Agent 进入预约流程 ===")
        mem["appointing"] = {"stage": "collect"}
        result = await _appointing_flow(state, mem, mem["appointing"])
        if result is not None:
            return result

    # 获取 MCP 工具(per-call 模式: Nacos 发现 -> 连接 -> listTools; 失败/挂起降级为空列表)
    mcp_tools = await _get_tools_safe()
    if not isinstance(mcp_tools, list):
        mcp_tools = []
    if not mcp_tools:
        print("警告：没有可用的 MCP 工具，权益服务降级")
        mem["answered"] = True
        return _rights_emit(
            mem, AIMessage(content="抱歉，权益服务暂时不可用，请稍后再试。"),
            "supervisor", {"active_agent": None},
        )

    # 已知登录用户 ID 时直接注入，避免 LLM 反问或编造
    # (防御式转 int: MCP get_right_list 要求整数, state 里可能是 str 或 int)
    user_id = state.get("user_id")
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        user_id = None
    if user_id:
        user_id_hint = f"当前登录用户 userId = {user_id}，查询权益时直接使用该 ID 作为 userId 参数，不要再向用户询问。"
    else:
        user_id_hint = "如需查询权益而用户未提供身份信息，请礼貌地向用户询问用户 ID。"

    # 查询输入: 权益私有记忆(镜像+摘要) + 全局背景摘要, 不再读全量主信箱
    agent = create_agent(
        model,
        mcp_tools,
        system_prompt=(
            RIGHTS_PROMPT.format(user_id_hint=user_id_hint)
            + background_block(state.get("global_summary"))
            + background_block(mem.get("summary"), title="此前权益服务记录摘要")
        ),
    )

    # 带权益视角对话历史，支持"查我的权益 -> 附近哪家店能用"这类连续诉求
    # recursion_limit=10: 内层 ReAct 循环上限, 防止 LLM 固执重复发同一 tool_call 撞上外层默认 10007
    response = await agent.ainvoke(
        {"messages": list(mem.get("messages") or [])},
        config={"recursion_limit": 10},
    )

    print(f"=======> rights response: {str(response)}")
    ai_response = response["messages"][-1] if response and response["messages"] else None
    mem["answered"] = True
    if not ai_response or not ai_response.content:
        return _rights_emit(
            mem, AIMessage(content="对不起，暂时无法处理您的权益请求，请稍后再试。"),
            "supervisor", {"active_agent": None},
        )

    print("=== rights Agent end ===")
    return _rights_emit(
        mem, AIMessage(content=ai_response.content), "supervisor", {"active_agent": None},
    )
