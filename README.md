# YiBot

> 基于 LangGraph 的多 Agent 医疗智能分诊与问诊平台
> 核心关键词：**分层编排 · 硬路由保证 · 双层安全 · 分层记忆 · MCP 工具生态 · 混合检索**

## 1. 项目简介

YiBot 面向医疗健康场景，通过 Supervisor + 子 Agent 分层编排提供四类核心能力：

| 能力 | 承载 Agent | 说明 |
|---|---|---|
| 症状采集 | `symptom_agent` | 5 维度结构化追问（部位/性质/时长/频率/诱因），独立进度判断器 + 3 轮硬上限 |
| 智能分诊 | `triage_agent` | P0~P3 紧急度分级 + 科室推荐，红旗症状强制走急救通道 |
| 健康科普 | `education_agent` | MCP 权威知识库检索（LLM 分组过滤）+ 本地 RAG 降级；未覆盖/涉及个人/越界 → 硬路由转「采集 → 分诊」 |
| 权益服务 | `rights_agent` | MCP 查权益/门店 + 跨请求多轮预约状态机（槽位抽取 → 核验 → 选店 → 下单） |

> 📐 详细设计文档见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)

## 2. 核心亮点

### 2.1 Supervisor 分层编排 + 状态标记硬路由
LLM 结构化输出（`SupervisorDecision`）做意图识别与 `Command` 动态路由，**关键链路不依赖 LLM 自觉**，由状态标记硬路由兜底，把「症状采集 → 分诊」链路的确定性从依赖 LLM 提升为系统保证：

- `pending_symptom_collect`：科普无法覆盖 / 越界 → 强制转症状采集
- `pending_triage`：症状采集完成 → 强制进分诊
- `appointing`：预约流程进行中 → 无条件回权益 Agent，防路由漂移
- 死循环守卫：无新用户输入的重复路由直接收尾，避免图空转

（`agent/supervisor.py`、`agent/sub_agent/rights_agent.py`）

### 2.2 双层安全机制（规则层 + LLM 层）
- **分诊**：P0/P1 红旗关键词前置正则筛查，规则层与 LLM 分级结果**取更紧急者**；P0 跳过常规问诊直接给出急救指引；信息不足时按保守（更紧急）级别处理（`agent/sub_agent/triage_agent.py`）
- **科普**：越界请求（开药/诊断/解读报告）正则前置拦截 + LLM `out_of_scope` 结构化判定二次兜底（`agent/sub_agent/education_agent.py`）

### 2.3 分层记忆架构（全局 + 私有）
- **全局记忆**：主信箱 15 轮滚动窗口，旧消息由 LLM 折叠进 `global_summary`（`RemoveMessage` 物理删除，控制 token）
- **私有记忆**：rights 独立镜像（12 条上限，尾部保留 8 条覆盖槽位抽取依赖）+ 私有摘要
- 路由 / 收尾等轻量调用只喂「窗口 + 摘要背景」，不携带全量历史；折叠失败全程降级，不阻塞主流程（`agent/struct/memory.py`）

### 2.4 跨请求多轮预约状态机
7 阶段状态机：`collect 槽位 → 权益匹配（"第一个/1" 序号消解）→ valid_right 核验 → 定位 → 门店推荐 → 选店 → appoint_service 下单`。每轮 `AppointingTurn` 结构化抽槽，含脏值防御（`"null"` 字面量视为未提供）、两次匹配失败委婉终止等服务化细节，状态随 Checkpointer 持久化跨请求存活（`agent/sub_agent/rights_agent.py`）

### 2.5 全链路 Pydantic 结构化输出
8 个 Schema 覆盖**所有决策点**：路由 / 采集进度 / 分诊结果 / 权益意图 / 槽位抽取 / 权益匹配 / 检索分组 / 科普结果，全程 `with_structured_output`，无裸文本解析（`agent/struct/schemas.py`）

### 2.6 企业级 MCP 工具链
Nacos 服务发现（v1/v2 兼容、鉴权 token、健康实例过滤、多实例随机负载）→ SSE 传输，失败自动降级 Streamable HTTP；SpringAI `structuredContent` 兼容 monkeypatch；工具输入 schema 自动转 Pydantic；per-call 建连 + 多层超时 + 降级不崩主流程（`tools/mcp.py`）

### 2.7 双通道混合检索（权威优先 + 本地兜底）
- MCP `search_knowledge_base` 优先：LLM 分析 `docGroup` 六大健康分组（心血管/呼吸/儿童/内分泌/女性/通用）做过滤，分组无命中自动退回全分组重检
- 本地 RAG 降级：Chroma 向量（通义 embedding）0.6 + BM25（jieba 分词）0.4 Ensemble 融合，top-5 硬截断防 prompt 爆 token
- **检索为空不编造**：如实告知暂无可靠资料，并转「采集 → 分诊」链路（`agent/sub_agent/education_agent.py`、`rag/core/hybird.py`）

### 2.8 双链路会话持久化
- **图状态链路**：`AIOMySQLSaver` Checkpointer（`thread_id = user-{uid}-{sid}`），多轮对话断点续聊
- **业务链路**：Redis 15min 滑动过期缓存 + 守护线程异步落库 MySQL，提供会话列表 / 回放接口（`database/conn.py`、`manager/session_manager.py`）

## 3. 系统架构

```
                React 前端（独立部署）
                        │  GET /chat/agent?question&session_id&user_id
                        ▼
      FastAPI（main.py · lifespan 构建单例 Graph · aiomysql 连接池）
                        │  graph.ainvoke(thread_id = user-{uid}-{sid})
                        ▼
┌──────────────────── LangGraph StateGraph 编排层 ────────────────────┐
│                                                                     │
│           ┌──────────────┐  意图识别(结构化输出) + Command 动态路由   │
│           │  supervisor  │◄─────────────────────────┐              │
│           └───┬────┬────┬┘                          │              │
│       ┌───────┘    │    └────────┐                  │              │
│       ▼            ▼             ▼                  │              │
│  symptom_agent  education_agent  rights_agent       │              │
│  5维症状采集     MCP知识库+RAG降级  权益查询/预约状态机 │              │
│       │(pending_triage 硬路由)      │                │              │
│       ▼                           ▼                 │              │
│  triage_agent ──P0──► emergency_response            │              │
│       │                                            │              │
│       └────────────────────────────────────────────┘              │
│                    final_response（收尾回复）                        │
│                                                                     │
│  硬路由链路保证：education 未覆盖/越界 → symptom 采集 → triage 分诊    │
└──────────────┬─────────────────────────────────┬───────────────────┘
               │ 工具与检索                       │ 状态与持久化
               ├─ MCP：Nacos 发现 → SSE(降级      ├─ AIOMySQLSaver checkpoints
               │   StreamableHTTP)               │   （图状态 · 断点续聊）
               │   get_right_list / valid_right  ├─ Redis 会话缓存
               │   get_nearby_stores /           │   （15min 滑动过期）
               │   appoint_service /             └─ MySQL session / session_message
               │   search_knowledge_base             （守护线程异步落库）
               ├─ 本地 RAG：Chroma(0.6)+BM25(0.4)
               └─ Tavily 联网搜索（备用）
```

## 4. 目录结构

```
YiBot/
├── main.py                  # FastAPI 入口，lifespan 构建 graph（单例）
├── agent/
│   ├── supervisor.py        # 主管：路由决策 + 硬路由 + 全局记忆折叠 + 图构建
│   ├── struct/
│   │   ├── consult_state.py # ConsultState 全局状态定义（TypedDict）
│   │   ├── schemas.py       # 8 个 Pydantic 结构化输出 Schema
│   │   └── memory.py        # 分层记忆工具（滚动窗口/折叠摘要/背景拼装）
│   └── sub_agent/
│       ├── triage_agent.py     # 分诊：关键词前置规则 + LLM 分级
│       ├── symptom_agent.py    # 症状采集：多轮追问 + 轮次硬上限
│       ├── education_agent.py  # 科普：越界拦截 + MCP 知识库 + RAG 降级
│       ├── rights_agent.py     # 权益：私有记忆 + 预约状态机 + MCP
│       ├── chit_chat_agent.py  # （未挂载，备用）
│       └── search_agent.py     # （未挂载，备用）
├── api/
│   ├── chat.py              # /chat/agent 图入口 + 会话历史接口
│   ├── user.py              # 用户 CRUD
│   └── rag.py               # 知识库管理
├── llm/                     # ChatOpenAI(GLM) 工厂、简单聊天/Agent（调试用）
├── manager/
│   ├── session_manager.py   # Redis 会话缓存 + MySQL 异步落库
│   └── rag_manager.py       # 知识库入库管理
├── rag/
│   ├── chroma/              # Chroma 客户端 + 元数据
│   ├── core/                # 混合检索（Chroma 0.6 + BM25 0.4）
│   └── loader/              # 文件/网页加载器
├── tools/
│   ├── mcp.py               # Nacos 发现 + SSE 连接 + 工具包装
│   ├── mcp_tool.py          # 本地 FastMCP Server（调试用）
│   ├── custom_tools.py      # 本地 mock 工具 + 工具错误中间件
│   └── tavily.py            # 联网搜索/网页提取
├── database/                # MySQL 连接、ORM、Repository
├── schemas/                 # 通用请求/响应 Schema
└── docs/ARCHITECTURE.md     # 技术架构文档
```

## 5. 技术栈

- **语言**：Python 3.13+
- **Web**：FastAPI（lifespan 内构建图，单例常驻）
- **编排**：LangGraph（`StateGraph` + `Command` 动态路由）、LangChain（`create_agent` ReAct）
- **LLM**：GLM-4.7（OpenAI 兼容协议接入），全程 `with_structured_output` 结构化决策
- **向量模型**：通义 `text-embedding-v3`
- **存储**：MySQL（SQLAlchemy / aiomysql / LangGraph `AIOMySQLSaver`）、Redis（会话缓存）、ChromaDB（向量库）
- **检索**：Chroma 向量召回 + BM25（jieba 分词）Ensemble 混合
- **工具**：MCP（Nacos 服务发现 + SSE / Streamable HTTP）、Tavily（联网搜索，备用）
- **前端**：React（独立部署，Node.js 24+）

## 6. 快速开始

### 6.1 环境依赖

- Python 3.13+
- 外部服务：MySQL 8.x、Redis、Nacos（8848，MCP Server 注册中心）
- MCP Server（SpringAI，注册名 `spring-ai-mcp-server`，SSE 端点 `/sse`，提供权益/门店/知识库工具）

### 6.2 安装依赖

```bash
python -m venv .venv
source .venv/bin/activate

pip install fastapi uvicorn python-dotenv \
    langchain langchain-openai langchain-classic langchain-community \
    langgraph langgraph-checkpoint-mysql langchain-mcp-adapters \
    mcp httpx langchain-tavily \
    sqlalchemy pymysql aiomysql redis \
    langchain-chroma chromadb jieba rank_bm25 bs4
```

### 6.3 配置环境变量

复制 `.env.dev` 为 `.env`，按需修改（完整变量说明见下表）：

| 变量 | 说明 | 必填 |
|---|---|---|
| `GLM_BASE_URL` / `GLM_MODEL_ID` / `GLM_API_KEY` | GLM 对话模型（OpenAI 兼容协议接入） | ✅ |
| `DASHSCOPE_BASE_URL` / `DASHSCOPE_MODEL_ID` / `DASHSCOPE_API_KEY` | 通义向量模型 `text-embedding-v3`（Chroma 嵌入） | ✅ |
| `DATABASE_URI` | SQLAlchemy 连接串（user/session 仓储），如 `mysql+pymysql://<user>:<pass>@127.0.0.1:3306/chatte` | ✅ |
| `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DB` | aiomysql 连接（LangGraph Checkpointer，本机 3306） | ✅ |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_PASSWORD` | Redis 会话缓存（默认 `127.0.0.1:6379`） | ✅ |
| `NACOS_SERVER_ADDR` / `NACOS_USERNAME` / `NACOS_PASSWORD` / `NACOS_NAMESPACE` / `NACOS_GROUP` | Nacos 服务发现与鉴权（默认 `127.0.0.1:8848` / `DEFAULT_GROUP`） | 使用 MCP 时 |
| `MCP_SERVICE_NAME` / `MCP_SSE_PATH` / `MCP_TIMEOUT` | MCP Server 注册名 / SSE 路径 / 超时秒（默认 `spring-ai-mcp-server` / `/sse` / `30`） | 可选 |
| `CHROMA_PERSIST_PATH` | Chroma 持久化目录（collection：`rag_knowledge`） | ✅ |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | 知识库文档切片参数（默认 `512` / `64`） | 可选 |
| `TAVILY_API_KEY` | 联网搜索（search_agent 专用，当前未挂载主图） | 可选 |

### 6.4 数据库初始化

1. 创建数据库（如 `chatte`）并执行下方「数据库表」中的建表 SQL；
2. `database/conn.py` 中 `checkpointer.setup()` 默认注释，**首次运行需临时打开**以创建 LangGraph `checkpoints` 表，建表成功后可注释回去。

### 6.5 启动后端

```bash
python -m uvicorn main:app --reload --port 8000

# API 文档：http://localhost:8000/docs
# MCP 全链路调试（Nacos→SSE→listTools→callTool）：python tools/mcp.py
```

### 6.6 前端

React 前端为**独立仓库部署**（本仓库仅后端），要求 Node.js 24+，通过 `/chat/*`、`/rag/*` 接口与本服务交互。

## 7. API 一览

### 聊天与多 Agent（`/chat`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/chat/agent` | 多 Agent 图入口（`question` / `session_id` / `user_id`），返回回复、紧急标记、活跃 Agent |
| GET | `/chat/llm` | 简单单轮聊天（调试用） |
| POST | `/chat/message/save` | 前端逐条上报消息（Redis 缓存 + 异步落库） |
| POST | `/chat/session/close` | 关闭会话 |
| GET | `/chat/session/list` | 用户历史会话分页列表 |
| GET | `/chat/session/history` | 会话消息回放（前端历史展示） |

### 知识库管理（`/rag`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/rag/save/file` | 上传文档 → 解析切片 → 向量入库 |
| POST | `/rag/save/web` | 网页内容抓取入库 |
| POST | `/rag/delete` | 按 `docId` 删除文档向量 |
| POST | `/rag/search` | 混合检索测试 |

### 用户管理（`/user`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/user/add` | 注册用户（手机号唯一校验） |
| GET | `/user/detail/mobile` | 按手机号查询用户 |
| POST | `/user/delete/{mobile}` | 按手机号删除用户 |

## 8. 数据库表

<details>
<summary>点击展开建表 SQL（user / session / session_message）</summary>

```sql
-- 用户表
create table if not exists user (
    `id` bigint unsigned NOT NULL AUTO_INCREMENT comment '主键ID',
    `name` varchar(200) DEFAULT NULL comment '用户名字',
    `full_name` varchar(512) DEFAULT NULL comment '用户全名',
    `password` varchar(200) NOT NULL comment '用户密码',
    `hash_pwd` varchar(200) DEFAULT NULL comment 'hash密码',
    `mobile` varchar(20) NOT NULL comment '用户手机号',
    `email` varchar(32) DEFAULT NULL comment '用户邮箱',
    `id_type` tinyint DEFAULT 0 comment '证件类型 取值：0-身份证 1-护照',
    `id_number` varchar(100) DEFAULT NULL comment '证件号码',
    `disabled` tinyint DEFAULT 0 comment '是否被禁用 取值：0-未禁用 1-已禁用',
    `gmt_create` datetime DEFAULT CURRENT_TIMESTAMP comment '创建时间',
    `gmt_update` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP comment '更新时间',
    `is_delete` tinyint DEFAULT 0 comment '删除标记 取值：0-未删除 1-已删除',
    `version` int DEFAULT 0 comment '版本号',
    PRIMARY KEY (`id`),
    KEY `idx_mobile` (`mobile`)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '用户表';

-- 会话主表
CREATE TABLE if not exists session (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `session_id` varchar(64) NOT NULL COMMENT '全局会话UUID',
  `user_id` varchar(64) NOT NULL COMMENT '用户ID',
  `status` varchar(16) NOT NULL DEFAULT 'active' COMMENT 'active/frozen/closed',
  `create_time` datetime DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `last_active_time` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后活跃时间',
  `close_time` datetime DEFAULT NULL COMMENT '关闭时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_session_id` (`session_id`),
  KEY `idx_user_time` (`user_id`,`last_active_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='会话主表';

-- 会话消息表
CREATE TABLE if not exists session_message (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `session_id` varchar(64) NOT NULL COMMENT '会话UUID',
  `type` tinyint DEFAULT 0 COMMENT '取值 0-用户消息 1-AI消息',
  `content` text COMMENT '消息内容',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`),
  KEY `idx_session_id` (`session_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='会话消息明细';
```

</details>

> ⚠️ `checkpoints` 系列表由 LangGraph `AIOMySQLSaver.setup()` 自动创建，无需手工建表。数据库账号密码请通过 `.env` 配置，勿提交真实凭据到仓库。

## 9. 演进路线

| 阶段 | 内容 | 状态 |
|---|---|---|
| S1 | Supervisor 全局汇总记忆（滚动窗口 + 折叠摘要） | ✅ 已交付并 e2e 验证 |
| S2 | rights 私有记忆（镜像 + 私有摘要 + 槽位状态机入记忆） | ✅ 已交付并 e2e 验证 |
| S3 | symptom / education / triage 同构私有记忆 | 待启动 |
| S4 | `rights_memory` Pydantic 类型化（state schema 强约束） | 待启动 |
| - | MCP 四层超时统一、外层 recursion_limit、路由职责描述扩充 | 待实施 |

## 10. 免责声明

本项目用于多 Agent 编排技术与医疗信息分诊场景的**学习与研究演示**。系统输出的分诊建议与科普内容均不能替代执业医师诊断，紧急情况请立即拨打 120 或前往最近医院急诊科。


