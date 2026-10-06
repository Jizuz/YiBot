# YiBot 平台技术架构文档

> 医疗场景多 Agent 智能问诊平台 —— 三端协同架构
> **Chatte 前端（React）· YiBot 后端（FastAPI + LangGraph）· MCP Tool Server（Spring AI）**
> 文档基线：2026-10-07（S1 全局记忆 + S2 rights 私有记忆已合入；知识库权威源管道已上线）

---

## 目录

1. [平台总览](#1-平台总览)
2. [YiBot 后端：多 Agent 编排核心](#2-yibot-后端多-agent-编排核心)
3. [Chatte 前端](#3-chatte-前端)
4. [MCP Tool Server（Java）](#4-mcp-tool-serverjava)
5. [核心数据流](#5-核心数据流)
6. [核心亮点](#6-核心亮点)
7. [已知限制与风险](#7-已知限制与风险)
8. [未来探索方向](#8-未来探索方向)
9. [部署与配置](#9-部署与配置)

---

## 1. 平台总览

### 1.1 系统拓扑

```
                        ┌─────────────────────────────┐
                        │      Chatte 前端 :5173       │
                        │  React 19 + TS + Vite 8      │
                        │  聊天 / 会话管理 / 登录       │
                        │  知识库文档管理页(Admin)      │
                        └──────┬───────────────┬──────┘
              /chat/* /user/*  │               │  /rag/admin/doc/*
              (JSON, SSE 预留) │               │  (JSON, Vite 代理)
                               ▼               ▼
        ┌──────────────────────────────┐   ┌───────────────────────────────┐
        │      YiBot 后端 :8000        │   │   MCP Tool Server :8090 (Java) │
        │  FastAPI + LangGraph 编排    │   │  Spring Boot + Spring AI MCP   │
        │  Supervisor + 4 子 Agent     │   │  ┌─ MCP 工具(权益/门店/天气/KB) │
        │  分层记忆 / 双层安全 / 状态机  │   │  ├─ 知识库权威源管道           │
        └───────┬──────────────┬───────┘   │  │  (kb_doc → MQ → 双索引)     │
                │              │           │  └─ 文档管理后台 REST           │
     ┌──────────┘              │  MCP 调用  └───┬────────────┬─────────────┘
     │ LangGraph Checkpointer  │  (SSE→Streamable│            │
     ▼                         ▼  HTTP, per-call)│            │
┌─────────┐  ┌───────┐   ┌─────────┐       ┌────▼────┐  ┌────▼─────┐  ┌──────────┐
│ MySQL   │  │ Redis │   │ Nacos   │       │ RocketMQ│  │ Qdrant   │  │ DashScope│
│ :3306   │  │ :6379 │   │ :8848   │       │ :8081   │  │ :6333    │  │ GLM/向量  │
│ chatte  │  │ 会话   │   │ 服务发现 │       │ proxy   │  │ 向量库    │  │ API      │
│ 库      │  │ 缓存   │   │         │       │ 文档事件 │  │ 1024维    │  │          │
└─────────┘  └───────┘   └─────────┘       └─────────┘  └──────────┘  └──────────┘
     ▲                                                        ▲
     └────────────── kb_doc 权威源表（两服务共用 chatte 库）──────┘
```

### 1.2 三端职责

| 端 | 技术栈 | 职责 |
|---|---|---|
| **Chatte 前端** | React 19 / TypeScript / Vite 8 / react-router 7 | 问诊对话交互、会话列表/回放、登录（手机号+密码，登录即注册）、知识库文档管理界面（Admin 页直连 MCP Server） |
| **YiBot 后端** | Python 3.13 / FastAPI / LangGraph / LangChain / GLM-4.7 | 多 Agent 编排核心：意图路由、症状采集、分诊、科普（调 MCP 检索）、权益预约状态机；图状态持久化、业务会话管理 |
| **MCP Tool Server** | Java / Spring Boot / Spring AI MCP / langchain4j / RocketMQ 5 / Qdrant | 对外暴露 6 个 MCP 工具（权益/门店/天气/知识库检索）；知识库权威源管道（文档 → MQ → 向量+BM25 双索引）；文档管理后台 |

### 1.3 基础设施依赖

| 组件 | 端口 | 用途 |
|---|---|---|
| MySQL 8（`chatte` 库） | 3306 | `user` / `session` / `session_message`（YiBot）；`kb_doc` 权威源 + `checkpoints`（LangGraph） |
| Redis | 6379 | 会话元数据/消息缓存（15min 滑动过期） |
| Nacos | 8848 | MCP Server 服务发现（健康实例过滤、多实例负载） |
| RocketMQ 5 (gRPC Proxy) | 8081 | 知识库文档变更事件（topic：`doc_chunk_vector_topic`，tag 区分事件类型） |
| Qdrant | 6333 | 向量库（collection：`doc_rag_collection`，1024 维） |
| DashScope | — | 对话 GLM-4.7（OpenAI 兼容）+ 向量 `text-embedding-v3`（两服务各自接入） |


---

## 2. YiBot 后端：多 Agent 编排核心

### 2.1 编排图（LangGraph StateGraph）

```
                          ┌────────────┐
                 user ──▶ │ supervisor │ LLM 结构化路由 + 关键词前置保证
                          └─────┬──────┘
        ┌───────────┬───────────┼───────────┬───────────────┐
        ▼           ▼           ▼           ▼               ▼
  ┌──────────┐ ┌─────────┐ ┌─────────┐ ┌───────────┐ ┌────────────┐
  │ symptom  │ │ triage  │ │education│ │  rights   │ │ chit_chat / │
  │ 症状采集  │ │ 分级分诊 │ │ 健康科普 │ │ 权益预约   │ │ search(未挂)│
  │ 多轮追问  │ │ P0~P3   │ │ MCP检索 │ │ 7阶段状态机│ │            │
  └────┬─────┘ └────┬────┘ └────┬────┘ └─────┬─────┘ └────────────┘
       └────────────┴─────┬────┴─────────────┘
                           ▼
                  ┌─────────────────┐
                  │ safety_guardian │ 最后一道防线（幻觉术语纠偏/紧急话术兜底）
                  └─────────────────┘
```

- **Supervisor 硬路由保证**（`agent/supervisor.py`）：关键词命中（急症/预约/科普等）直接指定 `next_agent`，LLM 仅在模糊意图时结构化决策——纯模型路由的漏判率被工程手段兜住。
- **双层安全**（`agent/struct/safety.py`）：正则黑名单/急症关键词前置拦截（快、确定）+ LLM 结构化判定兜底（慢、泛化），`need_emergency` 贯穿全图，任何节点可触发紧急话术。
- **分层记忆**（`agent/struct/memory.py`）：
  - S1 全局：15 轮滑窗 + 超窗折叠为摘要，防止长会话 token 爆炸；
  - S2 私有：`rights_memory` 镜像用户权益（列表/校验/锁定），跨轮预约流程中权益信息不丢失、不重复查询。
- **跨请求 7 阶段预约状态机**（`agent/sub_agent/rights_agent.py` + checkpointer）：
  `selecting_right → confirming_right → choosing_store → filling_info → submitting → submitted → appointing_success`，每阶段用户确认后推进，非法跳转被拒；下单成功进入 `submitted` 后跨请求等待机构确认。

### 2.2 类型与契约

- **8 个 Pydantic Schema**（`agent/struct/schemas.py`）：`AgentDecision` / `EmergencyCheck` / `SymptomSummary` / `TriageResult` / `EducationDraft` / `AppointmentInfo` / `ChatResponse` / `SessionBrief`——所有 LLM 输出走结构化解析，解析失败自动降级纯文本，接口层永不裸奔。

### 2.3 双链路会话持久化

```
LangGraph 图执行 ──▶ AIOMySQLSaver(checkpoints 表)   # 图状态/记忆/预约阶段，跨请求恢复
       │
       └──────▶ Redis 15min 滑动缓存（manager/session_manager.py）
                     └──▶ 异步批量落 MySQL(session / session_message)  # 业务侧会话列表/消息回放
```

两条链路解耦：图恢复不依赖业务表；业务回放不解析图状态。前端会话列表/历史接口（`/chat/session/*`）读业务链路。

### 2.4 MCP 客户端（`tools/mcp.py`）

- Nacos 按 `groupName/serviceName` 拉取**健康实例**列表，随机负载；
- 传输协商：优先 SSE（`/sse`），失败自动降级 Streamable HTTP（同一 URL），覆盖 Spring AI MCP Server 的 `STREAMABLE` 协议；
- **per-call 会话**：每次工具调用独立建立 MCP 会话用后即弃，无长连接状态，规避 keep-alive 断连与并发复用问题；
- 含 SpringAI 兼容 monkeypatch（新版 spring-ai SSE 注释格式对齐）；
- 本地兜底：`tools/mcp_tool.py`（FastMCP 天气工具）在 Nacos 不可用时以 stdio 起本地工具，开发环境零依赖。


---

## 3. Chatte 前端

> 独立仓库部署：`/Users/jizuz/WorkSpace/FRONT/Chatte`（本仓库 YiBot 仅后端）

### 3.1 技术栈与结构

React 19 + TypeScript + Vite 8 + react-router-dom 7 + lucide-react（图标），oxlint 检查。无状态管理库、无 UI 组件库——约 1500 行核心代码，全部 `useState`/`useRef`/`useCallback` 原生 Hooks。

```
src/
├── api/            # 5 个领域 API 模块（chat/session/user/message/knowledge）
│   └── chat.ts     # fetchChatReply(一次性) + streamChatReply(SSE 流式)
├── components/     # ChatWindow(对话窗) ConversationList(会话列表)
│                   # LoginModal(登录) Header KnowledgeBaseModal UploadDocModal
├── pages/          # AdminDocListPage（知识库文档管理页）
├── hooks/useAuth.ts# 登录态：localStorage 持久化 + 手机号/密码格式校验
├── config/index.ts # 双后端寻址（见 3.4）
└── App.tsx         # 顶层状态编排：消息/会话/流式中断/历史加载
```

### 3.2 会话双轨模型（App.tsx）

前端将消息归属分为两条轨道，避免"实时消息闪烁/丢失"与"历史回放"互相干扰：

- **实时会话**（`liveSessionIdsRef` 集合）：本次运行期间聊过的会话，切回时直接用本地内存消息，**不查数据库**；
- **历史会话**：首次切换时 `fetchSessionHistory` 从后端业务库回放（`userContent`/`agentContent` 成对展开）。

新会话采用**临时 ID 迁移**策略：先本地 `local-session-{ts}` 保证消息有稳定归属 → 首条消息经 `/chat/message/save` 换取服务端 `session_id` → 本地消息整体迁移到服务端 ID 并选中。登出时仅关闭本次新建会话、`AbortController` 中断进行中的流式回复。

### 3.3 SSE 流式已就绪（领先后端一步）

`api/chat.ts` 的 `streamChatReply` 已完整实现流式消费，当前后端 `/chat/agent` 返回一次性 JSON 时**自动降级**整体解析：

- 请求带 `Accept: text/event-stream`，按 `\n\n` 切分 SSE 事件（兼容 `\r\n` 跨块截断）；
- 负载解析兼容 **OpenAI 风格**（`choices[].delta.content`）与**自定义风格**（`{content, need_emergency, active_agent}`）；
- `[DONE]`/`DONE` 结束标记；`AbortSignal` 支持登出中断；
- 增量经 `onDelta` 回调实时填充气泡，`ChatWindow` 已有"对方正在回复…"占位与流式气泡渲染。

→ 后端补齐流式接口即可无缝点亮（见 [§8.1](#81-流式体验闭环优先级最高)）。

### 3.4 双后端寻址（config/index.ts）

| 地址 | 目标 | 用途 |
|---|---|---|
| `VITE_API_BASE_URL` | YiBot `:8000` | `/chat/*`、`/user/*`（问诊、会话、登录） |
| `VITE_RAG_API_BASE_URL` | MCP Server `:8090` | `/rag/admin/doc/*`（知识库文档管理，Admin 页直连 Java 服务） |

开发环境 RAG 地址为空串走 Vite 代理（`/rag → 127.0.0.1:8090`）规避跨域；生产直连。

### 3.5 登录即注册

`useAuth.login` → `loginOrRegister`（`/user/detail/mobile` 查询，404 则 `/user/add` 创建），手机号正则 `^1\d{10}$` 前端预校验，登录态仅存 localStorage。


---

## 4. MCP Tool Server（Java）

> 独立仓库：`/Users/jizuz/WorkSpace/JAVA/work/mcp-servers`（Spring Boot + Spring AI MCP，端口 8090）
> 双角色：**MCP 工具提供方**（YiBot 消费）+ **知识库权威源管道 & 管理后台**（前端 Admin 页消费）

### 4.1 对外 MCP 工具（6 个）

| 工具 | 类 | 说明 |
|---|---|---|
| `get_right_list` | RightTool | 查用户权益列表（当前 mock 数据，返回 `UserRightDTO` 结构化内容） |
| `valid_right` | RightTool | 权益有效性/库存校验（mock，恒 true） |
| `appoint_service` | RightTool | 提交预约（mock，打印后直接返回"预约成功"，**无落库、无回传通道**） |
| `get_nearby_stores` | LocationMapTool | 附近门店查询 |
| `queryWeather` | WeatherTool | 天气查询（wttr.in） |
| `search_knowledge_base` | RagTool | **知识库多路召回**：向量(Qdrant top10) + BM25(top10) → **RRF 融合（k=60）** → topK≤5，支持六大健康分组过滤（cardio/resp/ped/endo/women/common_living）；声明 `readOnly/idempotent` 注解；任一路失败自动降级另一路 |

### 4.2 知识库权威源管道（核心亮点）

```
管理后台 REST                 权威源（唯一事实）              事件驱动双索引
──────────────               ─────────────────              ──────────────────────────────
/rag/admin/doc/upload ──┐
/rag/admin/doc/crawl    ├─▶ DocumentParseManager ─┐
/rag/admin/doc/add     ─┘   (文件/网页/粘贴解析)    │
                                                ▼
/rag/admin/doc/update ──▶ KbDocService（唯一写入口）
                          ① 先落 MySQL kb_doc（分块 500/重叠 100，docId=UUID，md5 存档）
                          ② 服务端判定事件类型：md5/标题/分组任一变化才发 UPDATE（无变化不广播）
                          ③ 发 RocketMQ 事件（ADD/UPDATE/DELETE）
                          ✚ 上传场景 MQ 失败 → 回滚落库（调用方可安全重传）
                          ✚ 更新场景 MQ 失败 → 不回滚，等每日重建兜底收敛（权威源已最新）
                                                │
                              topic: doc_chunk_vector_topic（tag 区分事件）
                                                ▼
                     ┌──────────────────────────┴──────────────────────────┐
                     ▼ RagDocChangeConsumer（事件→索引同步）                ▼ DocVectorQdrantConsumer
                     · 幂等：msg_id 去重(上限10w) + 消息md5                 · 内存缓冲队列 + 批量刷盘
                       与权威源二次比对（乱序/过期消息跳过）                  (batch 20 / 1s)
                     · 失败重试 3 次 → 死信告警                              · DashScope text-embedding-v3
                     ▼                                                     · 1024 维写 Qdrant
              Qdrant 向量索引 + 本地 BM25 索引（KbDocIndexManager）          · Qdrant 不可用仅告警不阻断启动
                     ▲
                     └── 兜底：每日 03:00 BM25 从权威源全量重建
                         每周日 04:30 Qdrant 清空重灌（开关控制）
```

**设计要点**：

- **权威源单一写入口**：业务层无权指定事件类型，"内容没变也广播重建"在结构上不可能发生；
- **写路径分级容错**：新增走强一致（MQ 失败回滚），更新走最终一致（权威源已新、索引由定时任务收敛）——按业务风险选择一致性强度；
- **检索即融合**：`search_knowledge_base` 不依赖单一索引，向量与 BM25 互为降级备份，RRF 融合后输出 `hitFrom`（向量/关键词/向量+关键词）可解释来源。

### 4.3 文档管理后台（/rag/admin/doc/*）

分页列表（分组/状态/关键词筛选）、详情回显（分块拼全篇）、文件上传、网页 URL 抓取、粘贴新增、编辑（服务端 md5 判变化）、**软删除/恢复**（恢复重发 ADD 重建双索引）、手动触发 BM25 全量重建。Chatte 前端 `AdminDocListPage` 直连本组接口——**文档管理不经过 YiBot 后端**，管理面与对话面天然隔离。

### 4.4 服务注册（NacosDelayedRegister）

关闭 Spring Cloud 自动注册，`ApplicationReadyEvent` 后延迟 2 秒手动 `registry.register()`：规避 Nacos Client STARTING 时序 BUG，并强制覆盖注册端口（杜绝 `port=0` 注册）。YiBot 侧按健康实例过滤 + 随机负载消费。


---

## 5. 核心数据流

### 5.1 问诊主链路（一次提问的完整旅程）

```
用户输入
  → Chatte streamChatReply（GET /chat/agent?question&session_id&user_id）
  → YiBot api/chat.py：Redis 查会话 → 组装 ConsultState（含历史摘要+rights_memory）
  → LangGraph.invoke（checkpointer 以 session_id 为 thread_id 自动恢复图状态）
  → supervisor：关键词硬路由 / LLM 结构化路由 → 子 Agent
      · symptom：多轮采集 → SymptomSummary
      · triage：分级 P0~P3 → need_emergency
      · education：调 MCP search_knowledge_base（Nacos 发现 8090 → RRF 检索）→ 引用作答
      · rights：7 阶段状态机 + get_right_list/valid_right/appoint_service
  → safety_guardian 兜底 → ChatResponse（结构化）
  → 双链路落库（checkpoints / Redis→MySQL 异步）
  → 前端解析 reply + need_emergency + active_agent 渲染
```

### 5.2 知识入库链路（Admin 页 → 检索可命中）

```
AdminDocListPage 上传/粘贴/抓取
  → MCP Server /rag/admin/doc/* → KbDocService 权威源落库（kb_doc 分块+md5）
  → RocketMQ 事件 → RagDocChangeConsumer（幂等+二次比对）
  → 双索引：Qdrant 向量（异步批量 embedding）+ 本地 BM25
  → education Agent 下一轮 search_knowledge_base 即可召回（RRF 融合）
```

### 5.3 会话回放链路

```
用户切换历史会话 → 前端判定非实时会话
  → GET /chat/session/history → YiBot manager/session_manager
  → MySQL session_message 回放（Redis 未命中时）
  → userContent/agentContent 成对渲染
```

---

## 6. 核心亮点

跨三端提炼，均有代码出处：

| # | 亮点 | 出处 | 一句话价值 |
|---|---|---|---|
| 1 | **Supervisor 硬路由保证** | `agent/supervisor.py` | 关键词命中直接路由，LLM 只兜模糊意图——路由漏判被工程手段封死 |
| 2 | **双层安全防线** | `agent/struct/safety.py` | 正则前置（快）+ LLM 结构化兜底（泛化），医疗安全不许单点 |
| 3 | **分层记忆（S1 摘要折叠 + S2 权益私有镜像）** | `agent/struct/memory.py` | 长会话不爆 token；跨轮预约权益零丢失、零重复查询 |
| 4 | **跨请求 7 阶段预约状态机** | `agent/sub_agent/rights_agent.py` | checkpointer 驱动，对话中断/重启后流程精确续走 |
| 5 | **全链路 8 个 Pydantic Schema 契约** | `agent/struct/schemas.py` | LLM 输出全部结构化，解析失败自动降级，接口永不裸奔 |
| 6 | **企业级 MCP 链路（Nacos+SSE→Streamable 降级+per-call 会话）** | `tools/mcp.py` | 服务发现、传输协商、无状态调用，Python 侧兼容 Spring AI 特有格式 |
| 7 | **知识库权威源管道** | MCP Server `KbDocService`/`RagDocChangeConsumer` | 单一写入口 + 事件驱动 + 幂等/二次比对 + 定时兜底重建，索引终态收敛 |
| 8 | **混合检索（向量+BM25→RRF）与降级** | MCP Server `RagTool`；YiBot `rag/core/hybird.py`（Chroma 0.6 + BM25 0.4 本地降级） | 双路互为备份，`hitFrom` 可解释，空结果不编造 |
| 9 | **双链路会话持久化** | `manager/session_manager.py` + `AIOMySQLSaver` | 图状态与业务回放解耦，各自演进互不绑架 |
| 10 | **前端会话双轨 + 临时 ID 迁移** | Chatte `App.tsx` | 实时会话不查库、历史会话库回放；新会话 ID 平滑迁移无闪烁 |
| 11 | **前端 SSE 流式预留（含自动降级）** | Chatte `api/chat.ts` | 后端未流式也能跑，一旦升级零前端改动 |
| 12 | **管理面与对话面隔离** | Chatte Admin 页直连 MCP Server 8090 | 文档运维流量不打扰问诊链路 |


---

## 7. 已知限制与风险

| # | 限制 | 影响 | 位置 |
|---|---|---|---|
| 1 | 权益/预约 MCP 工具为 mock：`appoint_service` 无落库、无机构确认回传通道，`release_right`（释放权益）不存在 | 预约只到"提交成功"，超时取消/权益回滚全链路缺失（见 §8.2） | MCP Server `RightTool.java` |
| 2 | 后端 `/chat/agent` 未流式（一次性 JSON） | 长回复等待感明显；前端已备好 SSE 消费能力 | YiBot `api/chat.py` |
| 3 | 图内回复不自动落业务库，依赖前端逐条上报 `message/save` | 前端漏报/异常退出时历史回放缺消息（双链路不一致窗口） | YiBot `api/chat.py` + Chatte `App.tsx` |
| 4 | 接口无鉴权（user_id 明文参数）；前端登录态仅 localStorage | 任意 user_id 可读写他人会话，生产前必须补 token 鉴权 | 三端 |
| 5 | MCP 路径依赖环境变量对齐：YiBot 默认拼 `/sse`，MCP Server 实际为 STREAMABLE `/mcp`（靠 SSE 失败→降级同 URL streamable 才通） | 配置错误时多一次失败重连；建议 `MCP_SSE_PATH=/mcp` 显式声明 | YiBot `tools/mcp.py`、MCP Server `application.yml` |
| 6 | `graph.invoke` 未设 `config={"recursion_limit": ...}`；MCP 链路四层超时（Nacos/HTTP/MCP/LLM）未统一 | 极端循环或慢依赖时请求悬挂 | YiBot `api/chat.py`、`tools/mcp.py` |
| 7 | 工具调用无重试（网络抖动直接失败给到 LLM 兜底话术） | 可用性依赖单次成功率 | YiBot `tools/mcp.py` |
| 8 | `chit_chat` / `search_agent`（联网 Tavily）已实现未挂载到路由表 | 闲聊与联网搜索场景实际不可达 | YiBot `agent/supervisor.py` |
| 9 | MCP Server `application.yml` 提交了明文 DashScope API Key（及 Nacos/MySQL 弱口令） | **凭据泄露风险，须立即轮换并改环境变量注入** | MCP Server `application.yml` |
| 10 | 医疗合规审计缺失：无全链路操作留痕、无模型版本/prompt 版本快照 | 生产化前置项 | 三端 |


---

## 8. 未来探索方向

### 8.1 流式体验闭环（优先级：最高）

**现状**：前端 `streamChatReply` 已完整支持 SSE（增量渲染、[DONE]、AbortSignal、非流式自动降级）；后端 `/chat/agent` 仍是阻塞式一次性 JSON。

**改造**：FastAPI 侧新增（或改造为）`StreamingResponse(media_type="text/event-stream")`，LangGraph 侧以 `graph.astream_events(input, version="v2")` 逐 token 下发：

```
data: {"content": "您"}
data: {"content": "好"}
data: {"content": "，"}
...
data: {"content": "", "need_emergency": false, "active_agent": "symptom"}   # 尾帧带元信息
data: [DONE]
```

前端解析已兼容此格式，**零前端改动点亮**。配套收益：多轮预约的"正在查询门店…"等阶段事件也可作为 SSE 中间帧下发，体验从"等 10 秒出全文"变为"秒级首字+过程可见"。

### 8.2 预约全生命周期编排（Temporal，优先级：高）

**边界原则**：LangGraph 管**对话内**编排（秒级、轮内决策图）；Temporal 管**对话外**业务生命周期（下单成功后的长周期流程）。两者以旁路拆分集成，不把 Temporal 塞进图节点。

```
现状（断点）：appoint_service(mock) ──▶ "预约成功"（全链路终点）

目标（Temporal Workflow：AppointmentLifecycle）：
  下单成功 ──▶ Activity: 写 appointment 业务表(submitted/confirming)
           ├──▶ Timer(等机构确认, 如 2h)
           │      ├── Signal: 机构确认回调 → confirmed → 就诊前 24h Timer → 提醒 → 随访
           │      └── 超时 → 补偿: cancel + Activity: release_right 释放权益 → 通知用户
           └── Signal: 用户主动取消 → 同上补偿
```

**为什么是 Temporal 的标准场景**（四项全中）：长周期 Timer（小时/天级）、外部事件（Signal：机构回调/用户取消）、补偿事务（超时释放权益，失败自动重试至一致）、全程可观测（每个预约一条可追溯的执行历史）。

**落地前置条件**（均为业务决策，非技术问题）：
1. 机构确认结果回传方式：回调（MCP Server 暴露回调接口）还是轮询（补 `query_appoint_status` 工具）；
2. `release_right` 工具需在 MCP Server 实现（当前仅有 `valid_right` 校验）；
3. 提醒/随访触达通道：短信/推送，还是用户下次进入会话时由 Agent 主动发起（后者可复用 LangGraph `interrupt`）。

**触发时机**：预约日均千单以上 / 出现真实资损（超时未释放权益引发客诉）/ 出现第 2 条以上长周期流程时引入；此前可先用「MySQL 状态表 + APScheduler 扫描」过渡（schema 已规划：`status: submitted→confirming→confirmed/cancelled_timeout/cancelled_user→reminded→followed_up` + `confirm_deadline` + 幂等字段）。

### 8.3 记忆体系演进

- **S3（同构私有记忆）**：symptom / education / triage 各自增加私有记忆槽（症状采集断点续问、科普不重复推、分诊结论沉淀），复用 S2 `rights_memory` 的成熟模式；
- **S4（类型化记忆）**：`rights_memory` 从 dict 升级为 Pydantic 模型，schema 校验前置到记忆读写；
- **长期用户健康画像**：跨会话聚合（既往分诊史/过敏史/慢病标签），入库独立 `user_profile` 表，问诊时注入 Supervisor 与 symptom/triage 的上下文——从"记得这轮对话"进化到"记得这个用户"。


### 8.4 检索质量进阶

- **精排（Rerank）**：RRF 粗排 top10~20 后接交叉编码器精排（DashScope `gte-rerank` / 本地 bge-reranker），科普引用准确率有望显著提升；
- **查询改写**：多轮对话中用户问句常含指代（"那这个药呢"），education Agent 检索前用轻量 LLM 结合会话改写为独立查询（含 HyDE 方向探索）；
- **评测驱动**：建设检索评测集（query→应命中文档标注），召回率/MRR 纳入 CI，避免调参凭感觉；
- **索引侧**：BM25 中文分词优化（当前 Lucene Analyzer）、医学同义词表扩展（"心梗/心肌梗死/AMI"）。

### 8.5 安全与合规

- **鉴权**：JWT（登录颁发，`/chat/*`、`/rag/admin/*` 统一校验），前端 fetch 封装注入 Authorization；
- **Agent 评测与红队**：分诊准确率评测集（P0~P3 标注语料）、越权/诱导/幻觉红队用例回归；
- **审计合规**：全链路操作留痕（谁在何时问了什么、引用了哪篇文档、调用了哪个工具）、模型与 prompt 版本快照，满足医疗可追溯要求。

### 8.6 工程化与可观测

- **统一超时/重试**：MCP 四层超时对齐、工具调用引入 `tenacity` 指数退避、`graph.invoke` 补 `recursion_limit`（低垂果实，可先行）；
- **全链路追踪**：LangSmith 或 OpenTelemetry（trace_id 贯穿 Chatte→YiBot→MCP Server→Qdrant/MQ），慢轮次定位从"看日志猜"变为"看瀑布图"；
- **CI/CD 与容器化**：三端镜像化（前端 Nginx / YiBot uv / MCP Server JRE），docker-compose 一键起全套依赖（MySQL/Redis/Nacos/RocketMQ/Qdrant）。

### 8.7 能力扩展

- **挂载 chit_chat / search_agent**：路由表加两项即可激活闲聊与 Tavily 联网搜索（代码已就绪）；
- **多模态问诊**：报告单/患处照片上传（VLM 识别 → 注入 symptom 采集），老年人语音输入（ASR → 同链路）；
- **主动式健康管理**：结合 §8.2 的随访流程，Agent 在用户进入会话时主动发起随访追问、复诊提醒——平台从"被动应答"升级为"主动关怀"。

---

## 9. 部署与配置

### 9.1 端口与关键环境变量

| 服务 | 端口 | 关键配置 |
|---|---|---|
| Chatte 前端 | 5173（dev） | `VITE_API_BASE_URL`（YiBot）、`VITE_RAG_API_BASE_URL`（MCP Server） |
| YiBot 后端 | 8000 | `GLM_API_KEY`、`MYSQL_*`、`REDIS_*`、`NACOS_*`、`MCP_SSE_PATH`（建议 `/mcp`）、`RAG_SWITCH` 等 20+ 项，见 README「快速开始」 |
| MCP Tool Server | 8090 | `spring.ai.mcp.server`（STREAMABLE `/mcp`）、`rocketmq.proxy-endpoint`、`qdrant.rest-url`、`dashscope.*`、`rag.recall.*`（**API Key 应改环境变量注入**） |

### 9.2 启动顺序建议

```
1. 基础设施：MySQL → Redis → Nacos → RocketMQ(proxy) → Qdrant
2. MCP Tool Server（等 Nacos 注册完成，含 BM25 首建）
3. YiBot 后端（uvicorn main:app）
4. Chatte 前端（npm run dev）
5. 知识库预填充：Admin 页上传文档 → 观察 MQ 消费日志 → education Agent 验证召回
```

### 9.3 相关文档

- 产品与接口说明：[`/README.md`](../README.md)
- 前端仓库：`FRONT/Chatte`（详见其 README）
- MCP 工具服务仓库：`JAVA/work/mcp-servers`

---

*本文档基于代码现状编写，所有亮点与限制均标注代码出处；§8 为规划项，落地前请以最新代码为准。*







