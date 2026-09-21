# YiBot 技术架构文档

> 基于 LangGraph 的多 Agent 医疗问诊与权益服务平台
> 核心关键词:分层编排、多轮对话、规则安全、分层记忆
> 文档基线:2026-09-19(S1 全局记忆 + S2 rights 私有记忆已合入)

---

## 目录

1. [项目概览](#1-项目概览)
2. [总体架构](#2-总体架构)
3. [分层编排](#3-分层编排)
4. [多轮对话机制](#4-多轮对话机制)
5. [规则与安全机制](#5-规则与安全机制)
6. [分层记忆架构](#6-分层记忆架构)
7. [工具调用层](#7-工具调用层)
8. [会话与持久化](#8-会话与持久化)
9. [已知限制与演进方向](#9-已知限制与演进方向)

---

## 1. 项目概览

### 1.1 定位

YiBot 是一个医疗场景的多 Agent 智能问诊平台,提供四类核心能力:

| 能力 | 承载 Agent | 说明 |
|---|---|---|
| 症状采集 | `symptom_agent` | 5 维度结构化追问(部位/性质/时长/频率/诱因),轮次受限 |
| 智能分诊 | `triage_agent` | P0~P3 紧急度分级 + 科室推荐,红旗症状强制急救通道 |
| 健康科普 | `education_agent` | 混合检索(RAG) + 基于资料回答,越界请求拒答 |
| 权益服务 | `rights_agent` | MCP 工具查权益/门店 + 跨请求多轮预约状态机 |

另有 `chit_chat_agent`(MCP 通用工具 Agent)与 `search_agent`(Tavily 联网搜索)为早期/备用节点,**当前未注册进主图**。

### 1.2 技术栈

- **编排**:LangGraph(`StateGraph` + `Command` 动态路由)+ LangChain(`create_agent` ReAct)
- **LLM**:GLM-4.7(OpenAI 兼容协议接入,`temperature=0.7`),全程 `with_structured_output` 结构化决策
- **Web**:FastAPI(lifespan 内构建图,单例常驻)
- **存储**:MySQL(会话/消息表 + LangGraph `AIOMySQLSaver` Checkpointer)、Redis(会话缓存)、Chroma(向量库)
- **检索**:Chroma 向量召回 + BM25(jieba 分词)Ensemble 混合
- **工具**:MCP(Nacos 服务发现 + SSE)、Tavily、本地 mock 工具

### 1.3 目录结构

```
YiBot/
├── main.py                  # FastAPI 入口,lifespan 构建 graph(单例)
├── agent/
│   ├── supervisor.py        # 主管:路由决策 + 硬路由 + 全局记忆折叠 + 图构建
│   ├── checkpointer.py      # (空文件,保留位)
│   ├── struct/
│   │   ├── consult_state.py # ConsultState 全局状态定义(TypedDict)
│   │   ├── schemas.py       # 7 个 Pydantic 结构化输出 Schema
│   │   └── memory.py        # 分层记忆工具(滚动窗口/折叠摘要/背景拼装)
│   └── sub_agent/
│       ├── triage_agent.py     # 分诊:关键词前置规则 + LLM 分级
│       ├── symptom_agent.py    # 症状采集:多轮追问 + 轮次硬上限
│       ├── education_agent.py  # 科普:正则越界拦截 + RAG
│       ├── rights_agent.py     # 权益:私有记忆 + 预约状态机 + MCP
│       ├── chit_chat_agent.py  # (未挂载)
│       └── search_agent.py     # (未挂载)
├── api/
│   ├── chat.py              # /chat/agent 图入口 + 会话历史接口
│   ├── user.py              # 用户 CRUD
│   └── rag.py               # 知识库管理
├── llm/
│   ├── base_llm.py          # ChatOpenAI(GLM)工厂
│   ├── simple_chat_model.py # 简单单轮聊天
│   └── simple_agent.py      # 简单 ReAct Agent(调试用)
├── manager/
│   ├── session_manager.py   # Redis 会话缓存 + MySQL 异步落库
│   └── rag_manager.py       # 知识库入库管理
├── rag/
│   ├── chroma/              # Chroma 客户端 + 元数据
│   ├── core/hybird.py       # 混合检索器(Chroma 0.6 + BM25 0.4)
│   └── loader/              # 文件/网页加载
├── tools/
│   ├── mcp.py               # Nacos 发现 + SSE 连接 + 工具包装(per-call)
│   ├── mcp_tool.py          # 本地 FastMCP Server(高德天气,stdio)
│   ├── custom_tools.py      # 本地 mock 工具 + 工具错误中间件
│   └── tavily.py            # 联网搜索/网页提取
├── database/                # MySQL 连接、ORM、Repository
└── schemas/                 # 通用请求/响应 Schema
```

---

## 2. 总体架构

```
┌──────────────┐
│  React 前端   │
└──────┬───────┘
       │ GET /chat/agent?question&session_id&user_id
┌──────▼─────────────────────────────────────────────────────┐
│ FastAPI (main.py lifespan)                                  │
│   app.state.graph = build_graph(checkpointer)   ← 启动构建一次│
│   app.state.pool  = aiomysql 连接池                          │
└──────┬─────────────────────────────────────────────────────┘
       │ graph.ainvoke({messages, user_id, session_id},
       │              config={thread_id: "user-{uid}-{sid}"})
┌──────▼─────────────────────────────────────────────────────┐
│ LangGraph StateGraph(ConsultState)      [AIOMySQLSaver 持久]│
│                                                              │
│   START ──► supervisor ──┬──► triage_agent ──► emergency ─►END(P0)
│        (每轮先做全局记忆折叠) │        │            response      │
│                           │        └──► supervisor(回传)      │
│                           ├──► symptom_agent ─┬─► __end__(追问,│
│                           │                   │   等下轮用户输入)│
│                           │                   └─► supervisor  │
│                           │                      (采集完+标记) │
│                           ├──► education_agent ──► supervisor │
│                           ├──► rights_agent ─┬─► __end__(预约 │
│                           │                  │   追问/查询回复)│
│                           │                  └─► supervisor / │
│                           │                     final_response│
│                           └──► final_response ──────► END     │
│                                                              │
│  硬路由(优先于 LLM 路由):                                    │
│   · pending_triage=True        → 强制 triage_agent            │
│   · rights_memory.appointing   → 强制 rights_agent            │
└──────────────────────────────────────────────────────────────┘
       │ 工具调用
┌──────▼─────────────┬──────────────┬─────────────┐
│ MCP Server(SpringAI)│  RAG 检索     │  Tavily     │
│ Nacos→SSE per-call  │ Chroma+BM25  │ (备用Agent) │
│ get_right_list 等   │ education用   │             │
└────────────────────┴──────────────┴─────────────┘
```

**一次请求的完整链路**(`GET /chat/agent`):

1. 前端带 `question/user_id/session_id` 调用;
2. `api/chat.py` 组装 `thread_id = user-{user_id}-{session_id}`,从 checkpointer 恢复历史状态(含全部记忆字段);
3. 图从 `START` 进入 `supervisor`(每轮最先执行全局记忆折叠);
4. 硬路由优先 → 否则 LLM 结构化路由选择子 Agent;
5. 子 Agent 处理后以 `Command` 回 `supervisor` / `__end__`(等下轮用户输入)/ `final_response`;
6. `final_response` 生成收尾回复 → END → 返回 `reply / need_emergency / active_agent`;
7. 状态(含消息与记忆)由 checkpointer 写回 MySQL,下轮请求原样恢复。

---

## 3. 分层编排

### 3.1 两层结构:Supervisor + 子 Agent

系统采用经典的 Supervisor 模式,而不是对等 Agent 网格。**Supervisor 只做调度不做业务**,业务全部下沉到子 Agent,每层职责单一:

| 层 | 模块 | 职责 | 输出方式 |
|---|---|---|---|
| 编排层 | `supervisor_node` | 意图识别、动态路由、硬路由、全局记忆折叠 | `Command(goto=子Agent)` |
| 编排层 | `final_response_node` | 汇总生成最终回复 | 普通 dict 追加消息 |
| 编排层 | `emergency_response_node` | 急救话术兜底(P0 专用) | 普通 dict 追加消息 |
| 业务层 | 4 个子 Agent 节点 | 领域问答/流程/工具调用 | `Command(goto=supervisor/__end__/...)` |

### 3.2 图的构建(`build_graph`)

```python
g = StateGraph(ConsultState)
g.add_node("supervisor", supervisor_node)          # 调度
g.add_node("triage_agent", triage_agent_node)      # 分诊
g.add_node("symptom_agent", symptom_agent_node)    # 症状采集
g.add_node("education_agent", education_agent_node)# 科普
g.add_node("rights_agent", rights_agent_node)      # 权益
g.add_node("emergency_response", ...)              # 急救兜底
g.add_node("final_response", ...)                  # 收尾
g.add_edge(START, "supervisor")                    # 固定入口
g.add_edge("emergency_response", END)
g.add_edge("final_response", END)
return g.compile(checkpointer=checkpointer)        # MySQL 持久化
```

要点:

- **静态边只有 3 条**(入口 + 两个终点),其余流转全部由节点返回的 `Command(goto=...)` 在**运行时动态决定**——这是 LangGraph 1.x 的 `Command` 机制,子 Agent 可以"越级"跳转(如 `triage_agent` 直接 `goto="emergency_response"`,`rights_agent` 守卫直接 `goto="final_response"`),不必回到 supervisor 中转;
- **图在 `main.py` lifespan 中只构建一次**,常驻 `app.state.graph`;修改任何节点代码需重启 uvicorn 生效;
- checkpointer 采用 `AIOMySQLSaver`(aiomysql 连接池,`autocommit=True`),按 `thread_id` 隔离不同用户会话。

### 3.3 Supervisor 的路由决策

软路由(LLM 结构化输出)的输入组装刻意保持"轻":

```
SystemMessage: SUPERVISOR_PROMPT(含当前 active_agent)
             + background_block(global_summary)   ← 全局记忆摘要(背景)
Human/AI ×N : recent_rounds(messages, 15)         ← 最近 15 轮原始消息
```

输出 schema(`SupervisorDecision`):`route ∈ {triage_agent, symptom_agent, education_agent, rights_agent, FINISH}` + `reason`(用于日志)。路由 prompt 内嵌协作规则:信息足够分诊直接 triage、信息模糊先 symptom、采集完成后通常再 triage、无后续则 FINISH、主管不得直接回答医学问题。

### 3.4 硬路由:状态标记优先于 LLM 判断

纯 LLM 路由存在"软判断失灵"(漏分诊、漏回流程)的问题,系统用**两个状态标记做确定性保证**,且都排在 LLM 路由之前:

| 标记 | 写入方 | 触发位置 | 效果 |
|---|---|---|---|
| `pending_triage` | symptom_agent 采集完成时置 `True` | supervisor 最前 | 无条件 `goto=triage_agent` 并消费标记,保证"采集→分诊"链路不依赖 LLM |
| `rights_memory.appointing` | rights_agent 进入预约流程时写入 | supervisor 次前 | 预约流程未结束前,用户所有输入(补信息/选门店/取消)无条件回 rights_agent,避免被路由到别的 Agent 打断流程;若本轮内容与预约无关,rights_agent 内部自动回落查询流程 |

> 硬路由的 update 会与当轮记忆折叠的 update 合并(`{**(mem_update or {}), ...}`),保证折叠不因硬路由短路而丢失。

### 3.5 子 Agent 的三种返回路径

| 路径 | 含义 | 典型场景 |
|---|---|---|
| `goto="supervisor"` | 本轮业务完成,交还调度 | 分诊完成、科普回答完、权益查询完 |
| `goto="__end__"` | 本轮向用户抛出追问,**等待下一条用户消息**(跨 HTTP 请求的多轮) | symptom 追问、预约槽位追问 |
| `goto="emergency_response"` / `"final_response"` | 越级直达终点 | P0 急救、rights 死循环守卫触发 |

### 3.6 全局状态 `ConsultState`

所有节点共享同一个 TypedDict 状态(不做子图拆分,以**字段前缀做逻辑命名空间**):

```python
class ConsultState(TypedDict):
    messages: Annotated[list, add_messages]  # 主信箱:reducer 追加/RemoveMessage 删除
    user_id: int                             # 登录用户(权益工具直接使用)
    session_id: str
    next_agent / active_agent / agent_turn_count  # 路由与子 Agent 轮次
    need_emergency: bool                     # 红旗标记(API 返回给前端)
    summary / symptom_summary                # 诊前摘要 / 症状总结(triage 复用)
    pending_triage: bool                     # 硬路由标记
    global_summary: str | None               # S1:Supervisor 全局汇总记忆
    rights_memory: dict | None               # S2:rights 私有记忆(见第 6 章)
```

`messages` 使用 `add_messages` reducer:`Command(update={"messages": [...]})` 默认按 id 追加;传 `RemoveMessage(id=...)` 则从 checkpointer 持久化历史中删除——这是记忆折叠能"物理收缩"主信箱的机制基础。

---

## 4. 多轮对话机制

系统中的"多轮"同时存在于**三个嵌套层次**,理解层次关系是理解本系统的关键:

```
HTTP 请求 N(用户发一条消息)
 └─ LangGraph 图执行(外层循环:supervisor ⇄ 子 Agent,受 LangGraph recursion_limit 约束)
     └─ 子 Agent 内部 ReAct 循环(create_agent:LLM⇄Tool,受内层 recursion_limit=10 约束)
跨请求多轮 = 用户消息 N+1 到来时,checkpointer 按 thread_id 恢复全部状态(消息+记忆+流程槽位)
```

### 4.1 跨请求多轮(checkpointer 驱动)

- 会话标识 `thread_id = user-{user_id}-{session_id}`,每个 (用户, 会话) 一条独立的状态时间线;
- 每轮 `graph.ainvoke` 结束,`AIOMySQLSaver` 把整个 `ConsultState`(含 `global_summary`、`rights_memory`、预约槽位)写入 MySQL;
- 下一条用户消息到来时状态原样恢复,子 Agent 据此判断"我在等什么"(等手机号?等选店?);
- 典型依赖跨请求状态的多轮:symptom 的 `agent_turn_count`(追问轮次)、rights 的 `appointing` 槽位状态机。

### 4.2 症状采集多轮(symptom_agent)

```
轮1: 用户"我肚子疼" → symptom_agent 生成追问 → goto __end__(等用户)
轮2: 用户补充        → 追问轮次 +1 → 再追问 ...
轮N: agent_turn_count >= 3 → 硬上限强制总结
      → goto supervisor,update: symptom_summary + pending_triage=True
      → supervisor 硬路由 → triage_agent
```

- 5 个采集维度:部位、性质、持续时间、频率、诱因/缓解因素;prompt 约束**每轮只问一个维度**、不重复问;
- `should_continue_collecting` 用第二个 LLM(`CollectionStatus` 结构化输出)判断采集进度(collected/missing/next_question);
- **硬上限 `turn >= 3` 强制总结**是收敛的最终保证;判断器调用异常时有 fallback(轮次≥2 直接总结,否则问通用问题)。

### 4.3 权益预约多轮(rights_agent 状态机)

预约是全系统最复杂的多轮流程,一个**跨请求持久化的槽位状态机**:

```
用户:"我想用洁牙卡预约"
  └─ rights_agent 意图分流(RightsIntent: query/appointing)
      └─ appointing = {stage: "collect"}  ← 写入私有记忆,随 checkpointer 持久化

stage=collect      追问 姓名/手机号/预约日期(每轮一问,goto __end__)
      │ 必填齐备
      ▼
  get_right_list(userId) → RightMatch 匹配 rightId
      │ 匹配失败 → 列出权益让用户选(支持"第一个/1"序号应答),连续 2 次失败委婉终止
      ▼
  valid_right(rightId) → 保守判定(宁可少约不可误约),不过则终止
      ▼
stage=wait_location  追问所在城市/区域
      ▼
  get_nearby_stores(location) → 列出门店(支持序号选择)
      ▼
stage=wait_store   等用户选店
      ▼
  appoint_service(req={appointDatetime, mobile, rightId, userName})
      → success / fail / unknown → 委婉收尾,appointing=None 流程清空
```

每轮入口由 `AppointingTurn` 结构化抽取完成三件事:

1. **意图判断**:`cancel`(取消)/ `other`(与预约无关,回落查询流程)/ `continue`;
2. **槽位抽取**:仅取用户明确给出的内容,禁止推测;口语日期换算 `YYYY-MM-DD`,但**严禁默认填充今天**;序号应答("第一个")结合上一轮列表换算成实际权益/门店名(修复过的序号死循环即在此链路);
3. **生成下一个追问**(`next_question`,一次只问一项)。

槽位合并(`_merge_slots`)带脏值防御:GLM 偶尔把"未提供"输出为字面 `"null"/"none"` 字符串,一律视为未提供,防止脏值覆盖已收集槽位或传入下单接口。

### 4.4 子 Agent 内部多轮(ReAct)

`rights_agent`(查询流程)与未挂载的 `chit_chat/search` 用 `create_agent` 构建标准 ReAct Agent,LLM 自主决定工具调用序列(如先 `get_right_list` 再 `get_nearby_stores`)。内层显式设置 `recursion_limit=10`,防止 LLM 固执重复发同一 tool_call 时撞上外层极高的默认上限而长时间空转。

---

## 5. 规则与安全机制

医疗场景的安全底线是"**规则先行、LLM 兜底、宁紧勿松**"。系统内安全机制按层次分布如下:

### 5.1 分诊安全(triage_agent):规则层 + LLM 层取更严

**规则前置筛查(零延迟、确定性)**——在调用 LLM 之前,先对用户最新消息做关键词匹配:

```python
P0_KEYWORDS = ["胸痛", "呼吸困难", "昏迷", "抽搐", "大出血", "呕血", "便血",
               "口角歪斜", "半身不遂", "说不出话", "剧烈头痛", "意识不清", "口唇发紫"]
P1_KEYWORDS = ["高热不退", "持续呕吐", "咯血", "剧烈腹痛", "消瘦"]

def pre_triage_check(text): ...   # 命中 P0 词 → 直接 "P0";否则命中 P1 词 → "P1"
```

三层防线:

| 防线 | 逻辑 |
|---|---|
| 规则命中 P0 | **跳过 LLM 分诊**,直接 `goto=emergency_response` + `need_emergency=True`(API 返回给前端做强提示) |
| 规则 P1 ∧ LLM 给 P2/P3 | **规则层升级**:强制改判 P1,reasoning 追加"规则层升级:检测到高风险关键词"——规则与 LLM 结论**取更紧急者** |
| LLM 独立判 P0 | 走同样的紧急通道 |

LLM 层 prompt 亦内嵌保守原则:**信息不足以判断时按更紧急级别处理**;分诊不确诊疾病、不推荐药物剂量;紧急情况统一推荐急诊科。若已有 `symptom_summary`,拼入上下文避免重复采集。

### 5.2 科普越界拦截(education_agent):正则前置 + LLM 双保险

```python
OUT_OF_SCOPE_PATTERNS = [
    r"我(这|该|要)吃(什么|啥)药",          # 用药咨询
    r"帮我(看看|解读).*(报告|检查|化验)",    # 报告解读
    r"我(是不是|得了|这是).*(病|癌|炎)",     # 求诊断
    r"(给我|帮我).*(开药|处方|剂量)",        # 开方
]
```

- **第一层(正则)**:命中即不检索不生成,直接返回"涉及个人医疗建议,请咨询医生或分诊";
- **第二层(LLM)**:生成结果的 `out_of_scope` 字段为真时同样拒答——防止正则漏网;
- 回答强制基于 RAG 检索资料(检索为空时提示谨慎回答),尾部附"不能替代医生诊断"免责声明与来源列表。

### 5.3 权益流程安全(rights_agent)

| 机制 | 实现 | 风险针对 |
|---|---|---|
| 抽取禁推测 | prompt 强制"仅基于用户明确给出的内容,禁止推测编造";日期严禁默认填充今天 | LLM 幻觉伪造用户信息 |
| 脏值过滤 | `"null"/"none"` 字面字符串视为未提供 | GLM structured output 脏输出污染槽位 |
| 校验保守判定 | `_valid_passed`:返回文本含"失败/不可用/过期/已使用"→不通过;必须明确含"通过/可用"才通过,**无法判定视为不通过** | 误约过期/不可用权益 |
| 匹配失败熔断 | rightId 无法唯一确定时列出让用户选;**连续 2 次失败委婉终止**,不无限重试 | 匹配死循环 |
| 结果三态判定 | `_appoint_outcome`:success/fail/**unknown**(结果不明提示以短信为准,不谎报成功) | 下单结果误报 |
| 身份强校验 | user_id 无法解析为有效整数时,提示重新登录,**不代为猜测** | 错号下单 |
| 服务降级话术 | MCP 不可用/超时 → 统一委婉终止"本次先不为您预约",不半途挂死 | 工具链故障时错误下单 |

### 5.4 循环与资源防护

- **rights 死循环守卫**:Agent 刚回复完(`answered=True` 且主信箱末条是 AI 消息、无 tool_calls)却被再次路由进来——这是 supervisor 路由循环的特征。守卫直接 `goto=final_response`,不再连 MCP/调 LLM(否则 LangGraph 高默认 recursion_limit 下会空转数千轮直到崩图);
- **内层 ReAct 上限**:`recursion_limit=10`;
- **API 层中断防御**(`api/chat.py`):若图被静默中断、`messages[-1]` 仍是用户消息或内容为空,不原样回显,而是返回"处理中断请重试"+ `need_emergency` 状态;
- **MCP 超时**:获取工具与每次 callTool 均包 `asyncio.wait_for(15s)`,`CancelledError`(BaseException)显式捕获,失败一律降级不抛出。

### 5.5 降级策略汇总(设计原则:任何失败不阻断主流程)

| 故障点 | 降级行为 |
|---|---|
| 全局记忆折叠失败 | 本轮跳过,旧消息暂留,下轮再试 |
| rights 私有折叠摘要失败 | 硬截断丢弃头部,保住最新问答 |
| MCP 全链路失败 | 权益服务"暂时不可用"话术,图正常收尾 |
| symptom 采集判断器异常 | 按轮次 fallback(≥2 总结,否则通用追问) |
| 图静默中断 | API 层兜底回复 |

---

## 6. 分层记忆架构

> 2026-09 落地的 S1+S2 方案。设计取舍:维持共享 `ConsultState` + **字段前缀命名空间**(不做子图拆分),记忆分三层,读写权限各不相同。

### 6.1 三层记忆总览

```
┌─────────────────────────────────────────────────────────────┐
│ ConsultState(随 AIOMySQLSaver 按 thread_id 持久化)           │
│                                                              │
│ ① 主信箱 messages(滚动窗口)                                  │
│    · 物理存储全部"活跃"对话,保留最近 WINDOW_ROUNDS=15 轮       │
│    · 超窗旧消息被 supervisor 折叠删除(RemoveMessage)          │
│    · 写:所有节点   读:supervisor(窗口)/其余节点(全量=窗口)     │
│                                                              │
│ ② global_summary — Supervisor 全局汇总记忆 [S1]               │
│    · 主信箱折叠出的 ≤300 字滚动摘要,会话级事实库                │
│    · 写:仅 supervisor   读:supervisor 路由/收尾、rights 分流   │
│                                                              │
│ ③ rights_memory — rights 私有记忆 [S2]                        │
│    {summary, messages(镜像≤12), answered, appointing}         │
│    · 写:仅 rights_agent  读:supervisor 只读 appointing 做硬路由│
└─────────────────────────────────────────────────────────────┘
```

常量(`agent/struct/memory.py`):`WINDOW_ROUNDS=15`(主信箱轮数窗口)、`PRIVATE_MSG_LIMIT=12`(私有镜像条数上限)、`PRIVATE_KEEP_TAIL=8`(折叠后保留尾部条数,保证覆盖"上一问+最新答"等抽取依赖)。

### 6.2 S1:全局汇总记忆(supervisor 维护)

**折叠时机**:supervisor 每轮执行**最先**做记忆维护(`_maintain_global_memory`),保证无论本轮走硬路由还是软路由,输入都已收敛。

**折叠流程**:

```
messages 超过 15 轮窗口?
  ├─ 否 → 跳过
  └─ 是 → fold_part = 窗口外的旧消息
        ├─ 存在无 id 消息(无法 Remove)→ 降级:本轮跳过,下轮再试
        ├─ fold_messages(LLM 把 旧摘要+待归档对话 合并为新摘要,≤300字)失败
        │     → 降级:本轮跳过,旧消息暂留
        └─ 成功 → update = {RemoveMessage×N(物理删除旧消息), global_summary=新摘要}
                  该 update 与后续路由/硬路由的 update 合并下发
```

**摘要 prompt(`FOLD_PROMPT`)的保留要点**:身份信息(姓名/手机号)、症状与采集进度、分诊结论、已查权益及结果、预约办理进度、用户偏好与时间线;丢弃寒暄与重复;第三人称 ≤300 字。

**消费方**(输入组装模式 = `background_block(摘要) + recent_rounds(窗口)`,替代旧的全量历史):

- supervisor 软路由:背景摘要 + 最近 15 轮;
- `final_response` 收尾:同上;
- rights_agent 意图分流:全局摘要 + 私有镜像尾部。

> 副作用收益:triage/symptom/education 仍直接读 `state["messages"]`,但由于主信箱本身被折叠到 15 轮窗口,这些节点的输入 token 同样被物理封顶。

### 6.3 S2:rights 私有记忆(rights_agent 维护)

私有记忆解决两个问题:① 其他 Agent 的长对话污染权益上下文;② 预约槽位状态需要可靠的私有存储。

结构:`rights_memory = {summary, messages, answered, appointing}`,由"四件套"维护:

| 组件 | 职责 |
|---|---|
| `_init_mem` | 从 state 取出/初始化私有记忆(缺字段 setdefault 补全) |
| `_sync_mirror` | **幂等增量同步**:把主信箱尾部"最后一条 AI 之后新增的用户消息"补进私有镜像(按尾条消息 id 去重) |
| `_fold_mirror` | 镜像超 12 条 → 头部 LLM 折叠进 `summary`,留尾 8 条;摘要失败则**硬截断保尾部**(槽位抽取依赖最近问答) |
| `_rights_emit` | 统一出口:回复**双写**(主信箱 + 私有镜像),并把整个 `rights_memory` 写回 state 持久化 |

rights_agent 的所有 LLM 调用(意图分流取镜像尾 6 条、槽位抽取取尾 8 条、ReAct 查询取全镜像)都以私有镜像为对话来源,并拼两层背景:`background_block(global_summary)` + `background_block(私有 summary)`。

**权限约定**:`rights_memory` 只有 rights_agent 可写;supervisor 仅读 `appointing` 字段做硬路由,形成单向依赖,避免记忆双写冲突。

### 6.4 记忆读写权限矩阵

| 字段 | supervisor | triage | symptom | education | rights | API |
|---|---|---|---|---|---|---|
| `messages` | 读写(折叠+窗口读) | 读 | 读写 | 读 | 读写(经镜像+双写) | 只取末条 |
| `global_summary` | **读写** | - | - | - | 读 | - |
| `rights_memory` | 只读 `appointing` | - | - | - | **读写** | - |
| `symptom_summary`/`pending_triage` | 读写(消费标记) | 读 | 写 | - | - | - |

### 6.5 长会话下的信息流(示例)

```
第 1~15 轮: 主信箱全量保存,三层记忆无折叠
第 16 轮起: supervisor 折叠最旧一轮 → global_summary 逐步累积事实
            主信箱恒 ≈15 轮 → 所有读全量历史的节点 token 恒定
rights 视角: 用户穿插咨询权益时,镜像只含权益相关往返(≤12 条)
            更早的权益对话在私有 summary 里,跨 Agent 的对话在 global_summary 里
预约期间:   appointing 槽位随 rights_memory 整体持久化,重启/换轮不丢
```

---

## 7. 工具调用层

### 7.1 MCP 工具链(`tools/mcp.py`)

```
Nacos 服务发现(v1/v2 兼容,鉴权 token,健康实例过滤,多实例随机负载)
  → 拼接 SSE URL(http://{ip}:{port}/sse)
  → sse_client 建连 + ClientSession.listTools
  → 输入 schema 转 Pydantic args_model → 包装为 McpRemoteTool(BaseTool)
```

- **per-call 建连模式**:`get_mcp_tools` 中连接用完即关;工具实际执行时各自独立建连——无长连接保活负担,代价是每次调用多一次握手;
- **SpringAI 兼容 patch**:SpringAI MCP Server 会把 List 结果直接放进 `structuredContent`(规范要求 object),启动时 monkeypatch 放宽该字段类型为 `Any`,否则 callTool 解析报错但服务端实际已执行;
- 工具获取失败返回空列表(主流程不崩),rights 侧再包 15s `wait_for` 超时;
- 当前使用的远端工具:`get_right_list`(查权益)、`get_nearby_stores`(附近门店)、`valid_right`(权益校验)、`appoint_service`(预约下单)、`queryWeather`(天气)。

### 7.2 RAG 检索(`rag/core/hybird.py`)

- Chroma 向量召回(通义 `text-embedding-v3`,collection `rag_knowledge`)权重 0.6 + BM25 关键词召回(jieba 分词,k=5)权重 0.4,`EnsembleRetriever` 融合;
- 结果硬截断 top-5 防 prompt 爆 token;检索为空时格式化为"未检索到资料,谨慎回答"提示;
- 仅 `education_agent` 使用;向量库数据经 `manager/rag_manager` + `api/rag.py` 管理(文件/网页加载器在 `rag/loader/`)。

### 7.3 其他工具

- `tools/tavily.py`:联网搜索/网页提取(`search_agent` 专用,当前未挂载);
- `tools/custom_tools.py`:本地 mock 工具(天气/计算/时间)+ `handle_tool_errors` 工具错误中间件(把异常转成给模型的 ToolMessage 而非崩溃);
- `tools/mcp_tool.py`:本地 FastMCP Server(高德天气,stdio 传输),可作独立 MCP 服务端调试。

---

## 8. 会话与持久化

系统存在**两条并行的会话数据链路**(注意区分):

### 8.1 图状态链路(LangGraph 原生)

- `thread_id = user-{user_id}-{session_id}` → `AIOMySQLSaver` 自动读写 checkpoints 表;
- 存的是**图状态**(消息、记忆、槽位、标记),服务于多轮推理,不直接给前端展示。

### 8.2 业务会话链路(SessionManager)

- **Redis**(`SESSION_EXPIRE_SECONDS=900`,15 分钟滑动过期):
  - `agent:user:{uid}:session` → 当前活跃 session_id;
  - `agent:session:{sid}` → 会话元数据;`agent:context:{sid}` → 消息缓存 list;
- **MySQL**(`session` / `session_message` 表):`@async_db_save` 装饰器以**守护线程异步落库**,不阻塞请求;会话状态机 `active → frozen/closed`;
- 接口:`/chat/message/save`(前端逐条上报)、`/chat/session/close`、`/chat/session/list`(分页历史)、`/chat/session/history`(React 回放);
- **现状注意**:图内回复不会自动写入 `session_message`,依赖前端上报,两条链路可能存在不一致窗口。

---

## 9. 已知限制与演进方向

### 9.1 已知限制

1. **`api/chat.py` 未设置外层 `recursion_limit`**:LangGraph 默认上限极高,极端路由循环靠 rights 守卫兜底,但仍建议显式 `config={"recursion_limit": 50}` 快速失败;
2. **symptom_agent 采集完成判定弱**:`should_continue_collecting` 的 `decision` 字段当前未参与分支判断(代码以对象真值分支),采集收敛实际依赖 3 轮硬上限;
3. **MCP 四层超时未统一**:Nacos/连接/listTools/callTool 各自为政,rights 侧的 `wait_for` 15s 属临时补丁;
4. **模糊预约表述路由召回不足**:supervisor 职责描述未覆盖"我想预约一下洁牙"这类模糊表述,可能被路由到 symptom_agent(职责描述扩充待拍板);
5. **chit_chat / search_agent 未挂载**;`agent/checkpointer.py` 为空文件;
6. **图单例**:lifespan 构建一次,任何节点代码改动需重启 uvicorn;
7. **rights_memory 为弱类型 dict**(S4 计划 Pydantic 类型化);
8. symptom/triage/education 尚无私有记忆(S3 同构迁移)。

### 9.2 演进路线(已规划)

| 阶段 | 内容 | 状态 |
|---|---|---|
| S1 | Supervisor 全局汇总记忆(滚动窗口+折叠摘要) | ✅ 已交付并 e2e 验证 |
| S2 | rights 私有记忆(镜像+私有摘要+槽位状态机入记忆) | ✅ 已交付并 e2e 验证 |
| S3 | symptom/education/triage 同构私有化 | 待启动 |
| S4 | `rights_memory` Pydantic 类型化(state schema 强约束) | 待启动 |
| - | MCP 统一四层超时、外层 recursion_limit、路由职责描述扩充 | 待拍板/实施 |

### 9.3 部署与运行

```bash
# 依赖:Nacos(8848)、MCP Server(SpringAI, 注册名 spring-ai-mcp-server, SSE /sse)、
#       MySQL(chatte 库, checkpoints 表首次需 checkpointer.setup())、Redis(6379)、Chroma 持久目录

python -m uvicorn main:app --reload --port 8000
# 调试 MCP 链路: python tools/mcp.py  (Nacos→SSE→listTools→callTool 全链路演示)
```

环境变量(GLM 接入、Nacos、MySQL、Redis、DASHSCOPE 向量、Chroma 路径等)统一由 `.env` 提供,`load_dotenv()` 加载。
