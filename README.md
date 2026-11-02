# YiBot

> 基于 LangGraph 的多 Agent 医疗智能问诊平台 —— **编排智能 × 生产级底座**
>
> 分层编排 · 硬路由保证 · 双层安全 · 分层记忆 · MCP 工具生态 · 混合检索 ·
> JWT 双令牌 · APISIX 限流熔断 · LLM Token 配额 · 全链路可观测 · TDD 106 测试

## 1. 项目简介

YiBot 面向医疗健康场景，通过 Supervisor + 子 Agent 分层编排提供四类核心能力，并配套完整的生产级横切设施（鉴权 / 流量治理 / 成本管控 / 可观测），可直接作为多 Agent 应用的工程化参考实现：

| 能力 | 承载 Agent | 说明 |
|---|---|---|
| 症状采集 | `symptom_agent` | 5 维度结构化追问（部位/性质/时长/频率/诱因），独立进度判断器 + 3 轮硬上限 |
| 智能分诊 | `triage_agent` | P0~P3 紧急度分级 + 科室推荐，红旗症状强制走急救通道 |
| 健康科普 | `education_agent` | MCP 权威知识库检索（LLM 分组过滤）+ 本地 RAG 降级；未覆盖/涉及个人/越界 → 硬路由转「采集 → 分诊」 |
| 权益服务 | `rights_agent` | MCP 查权益/门店 + 跨请求多轮预约状态机（槽位抽取 → 核验 → 选店 → 下单） |

> 📐 架构细节见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)；生产化改造全过程见 [`技改.md`](技改.md)；网关演练记录见 [`deploy/DRILL.md`](deploy/DRILL.md)。

## 2. 亮点总览

| 维度 | 亮点 | 一句话 |
|---|---|---|
| **编排智能** | 状态标记硬路由 | 关键链路由系统保证，不依赖 LLM 自觉 |
| | 双层安全（规则+LLM） | 红旗症状正则前置，与 LLM 分级取更紧急者 |
| | 分层记忆 | 全局滚动窗口 + LLM 折叠摘要 + rights 私有镜像 |
| | 跨请求预约状态机 | 7 阶段槽位抽取，Checkpointer 持久化跨请求存活 |
| | 全链路结构化输出 | 8 个 Pydantic Schema 覆盖所有决策点，无裸文本解析 |
| **生产级底座** | JWT 双令牌鉴权 | 双密钥分离 + Redis 原子轮换 + 登出即吊销 + 前端单飞续期 |
| | APISIX 流量治理 | 分层限流（并发/速率/计数）+ 熔断半开 + CORS 收敛 |
| | LLM Token 配额 | 全局回调一次覆盖全部调用点，前置拦截超限不进图 |
| | 全链路可观测 | 结构化 JSON 日志 + trace_id 贯穿 + 统一响应/业务码 |
| | 工程质量 | TDD 106 测试全绿 · 依赖方向静态检查 · Alembic 迁移 · 密钥零入库 |

## 3. 核心亮点详解

### 3.1 Supervisor 分层编排 + 状态标记硬路由

LLM 结构化输出（`SupervisorDecision`）做意图识别与 `Command` 动态路由，**关键链路不依赖 LLM 自觉**，由状态标记硬路由兜底，把「症状采集 → 分诊」链路的确定性从依赖 LLM 提升为系统保证：

- `pending_symptom_collect`：科普无法覆盖 / 越界 → 强制转症状采集
- `pending_triage`：症状采集完成 → 强制进分诊
- `appointing`：预约流程进行中 → 无条件回权益 Agent，防路由漂移
- 死循环守卫：无新用户输入的重复路由直接收尾，避免图空转
- `recursion_limit` 硬上限兜底，异常场景不失控

（`orchestration/supervisor.py`、`orchestration/sub_agent/rights_agent.py`）

### 3.2 双层安全机制（规则层 + LLM 层）

- **分诊**：P0/P1 红旗关键词前置正则筛查，规则层与 LLM 分级结果**取更紧急者**；P0 跳过常规问诊直接给出急救指引；信息不足按保守级别处理（`orchestration/sub_agent/triage_agent.py`）
- **科普**：越界请求（开药/诊断/解读报告）正则前置拦截 + LLM `out_of_scope` 结构化判定二次兜底；**检索为空不编造**，如实告知并转「采集 → 分诊」（`orchestration/sub_agent/education_agent.py`）

### 3.3 分层记忆架构（全局 + 私有）

- **全局记忆**：主信箱 15 轮滚动窗口，旧消息由 LLM 折叠进 `global_summary`（`RemoveMessage` 物理删除，控制 token）
- **私有记忆**：rights 独立镜像（12 条上限，尾部保留 8 条覆盖槽位抽取依赖）+ 私有摘要
- 路由 / 收尾等轻量调用只喂「窗口 + 摘要背景」；折叠失败全程降级，不阻塞主流程（`orchestration/struct/memory.py`）

### 3.4 跨请求多轮预约状态机

7 阶段状态机：`collect 槽位 → 权益匹配（"第一个/1" 序号消解）→ valid_right 核验 → 定位 → 门店推荐 → 选店 → appoint_service 下单`。每轮 `AppointingTurn` 结构化抽槽，含脏值防御、两次匹配失败委婉终止等服务化细节，状态随 Checkpointer 持久化跨请求存活（`orchestration/sub_agent/rights_agent.py`）

### 3.5 企业级 MCP 工具网关

进程级网关（`tools/mcp_gateway/`）替代 per-call 建连：

- **订阅式服务发现**：后台协程周期拉取 Nacos 实例（v1/v2 兼容、鉴权 token 缓存），diff 通知上下线，拉取失败保留旧快照不清空
- **实例注册表**：closed → open → half_open 熔断状态机 + 三策略负载均衡（round_robin / random / least_conn 在途计数）
- **工具收敛**：跨实例聚合去重、TTL 缓存、动态生成 Pydantic 参数模型
- 调用链：本地参数校验 fail-fast → LB 选实例 → 失败换实例重试 → 成败上报驱动熔断；`stats()` 全链路观测
- SSE → Streamable HTTP 降级建连、SpringAI `structuredContent` 兼容

### 3.6 双通道混合检索（权威优先 + 本地兜底）

- MCP `search_knowledge_base` 优先：LLM 分析 `docGroup` 六大健康分组做过滤，分组无命中自动退回全分组重检
- 本地 RAG 降级：Chroma 向量 0.6 + BM25（jieba）0.4 Ensemble 融合，top-5 硬截断（`data/vectorstore/`）

### 3.7 JWT 双令牌鉴权（前后端全链路）

| 设计点 | 实现 |
|---|---|
| 双密钥分离 | access/refresh 各自独立 HS256 密钥，令牌类型交叉验签必然失败 |
| 校验链 | 签名 → exp → type → 黑名单 → DB 禁用复核（30min 窗口内封号即时生效） |
| refresh 原子轮换 | Redis `GETDEL auth:refresh:{jti}`，单次有效、并发仅一方成功 |
| 登出即吊销 | access jti 入黑名单（TTL=剩余有效期）+ refresh 白名单删除 |
| 前端体验 | access 仅驻内存（XSS 面最小）；401 单飞续期 + 请求重放，用户无感知 |
| 登录即注册 | 手机号不存在自动创建（仅存 bcrypt 哈希）；错误不区分存在性（防撞库） |
| 降级 | Redis 白名单写失败 → 仅发 access（`refresh_granted=false`），登录不阻断 |
| 止血开关 | `AUTH_ENABLED=false` 应急放行；query user_id 过渡期兼容通道（带告警） |

（`core/security.py`、`api/deps/auth.py`、`api/routes/auth.py`、前端 `src/api/{http,tokenStore,auth}.ts`）

### 3.8 APISIX 网关流量治理

分层限流保护长耗时 LLM 链路，后端 8000 不对外暴露（配置见 `deploy/`，etcd 模式一键注入 `scripts/apisix_admin_setup.py`）：

| 路由 | 策略 |
|---|---|
| `/chat/agent` | 并发上限 + 速率（rate=2/burst=5）+ 熔断（3 失败 → 503 快速失败 → 10s 半开恢复） |
| `/user/login`、`/token/refresh` | 按 IP 20 次/分，**Redis 跨实例共享计数**（防撞库爆破） |
| `/rag/*` | 从严限速 5/s |
| 全局 | CORS 白名单收敛至网关；SSE 长连接参数预置（流式化即开即用） |

演练实录（`deploy/DRILL.md`）：登录第 21 次 429、60 并发 54×429、熔断 503 快速失败 32ms、半开自动恢复——全部通过。

### 3.9 LLM Token 配额（成本可控）

- **全局计量**：模型层挂单一 `CallbackHandler`，一处覆盖 supervisor / 4 子 Agent / 记忆折叠 / 路由全部调用点；跨事件循环安全（线程回调经队列 + 常驻 worker 入账，e2e 验证）
- **前置拦截**：`/chat/agent` 入口、进图之前检查当日用量，超限返回 429 业务码 + 友好话术，**编排零调用**（成本不再增长）
- 按用户按日 Redis 计数（TTL 48h 对账窗口）；剩余量低于阈值随回复预警；`QUOTA_ENFORCE=false` 观察模式
- e2e 实测：三链路回归全程 token 精确入账（37775/50000）

### 3.10 全链路可观测 + 统一契约

- **结构化 JSON 日志**：自定义 LogRecord 工厂为每条日志注入 `trace_id`（contextvar 贯穿请求→编排→模型→数据），敏感字段（password/token/key）自动脱敏
- **统一响应**：`{code, msg, data, trace_id}` 全接口一致；业务码段化（40101~40105 鉴权 / 42901 配额 / 50000 系统）；全局异常兜底不泄堆栈；`BizError` 携带精确业务码
- trace_id 支持前端 `X-Request-Id` 透传，随响应头回传

### 3.11 工程质量与安全治理

- **四层架构 + 静态强制**：`api → orchestration → model/tools → data` 单向依赖，`scripts/check_layering.py` 静态检查阻断反向引用；横切层（core/config）零反向依赖
- **TDD**：pytest **106 项全绿**，红→绿全程可追溯；模型单例懒代理化（`set_model` 注入 mock）
- **安全治理**：密钥零入库（`pydantic-settings` 集中 + `.env.example` 模板 + 全库扫描）、数学表达式 ast 白名单求值（注入样例测试覆盖）、密码 bcrypt 不停机迁移（条件更新防并发双写）、会话数据隔离（属主校验）、`print` 清零、异步客户端全链路（阻塞事件循环零容忍）
- **数据迁移**：Alembic 版本管理 + mobile 唯一索引（前置去重脚本）+ 明文列删除（备而不发）+ 清尾扫描告警
- **12 个 Prompt 集中管理**（`orchestration/prompts/`，逐字版本化），为医疗审计快照打基础

## 4. 系统架构

```
            Chatte 前端 :5173（React 19 · authFetch 注入 Bearer · 401 单飞续期）
                        │  Authorization: Bearer {access_token}
                        ▼
            ┌──────────────────────────────┐
            │        APISIX 网关 :9080      │  limit-concurrency / limit-req /
            │  （唯一入口，8000 不外露）      │  limit-count(redis) / api-breaker / cors
            └──────────────┬───────────────┘
                           ▼
┌─ 接入层 api/ ──────────────────────────────────────────────────┐
│ routes(chat/user/rag/auth) · deps/auth(get_current_user)       │
│ responses(统一响应/业务码/全局异常/trace_id 中间件)              │
├─ 横切 core/ + config/ ─────────────────────────────────────────┤
│ security(JWT双令牌/bcrypt) · quota(配额) · logging(JSON+脱敏)  │
│ biz_codes · settings(集中配置)                                  │
├─ 编排层 orchestration/ ────────────────────────────────────────┤
│ supervisor(硬路由) · sub_agent×4 · struct(状态/记忆/Schema)    │
│ prompts(12个集中管理) · model_provider(懒代理可注入)            │
├─ 模型层 model/ ────────────────────────────────────────────────┤
│ provider(工厂:温度/超时/重试可配) · callbacks/token_meter       │
├─ 工具层 tools/（独立层，禁止反向依赖）──────────────────────────┤
│ mcp_gateway(Nacos订阅/熔断/LB/工具收敛) · tavily · custom       │
├─ 数据层 data/ ─────────────────────────────────────────────────┤
│ mysql(repo统一会话/受控落库线程池/checkpointer) · redis_client  │
│ session_manager(全async) · vectorstore(Chroma+BM25混合检索)     │
└────────────────────────────────────────────────────────────────┘
        │                │                  │
        ▼                ▼                  ▼
     MySQL 8          Redis 6            Nacos + MCP Server
  (业务+Checkpointer) (会话/令牌/配额)    (Java/Spring AI·权益/门店/知识库)
```

**依赖方向（静态检查强制）**：`api → orchestration → model/tools → data`，单向不可逆；`core/`、`config/` 为横切层被各层引用但不依赖业务层。

## 5. 目录结构

```
YiBot/
├── main.py                  # lifespan 组装（graph/redis/checkpointer/meter worker/mcp网关）
├── config/settings.py       # pydantic-settings 集中配置（30+ 环境变量单一来源）
├── api/                     # 【接入层】routes/ + deps/(auth,db) + responses
├── core/                    # 【横切】security / quota / logging / biz_codes
├── orchestration/           # 【编排层】supervisor + sub_agent×4 + struct + prompts/
├── model/                   # 【模型层】provider + callbacks/token_meter
├── tools/                   # 【工具层】mcp_gateway + tavily + custom_tools(ast安全求值)
├── data/                    # 【数据层】mysql/ + redis_client + session_manager + vectorstore/
├── schemas/                 # API DTO
├── alembic/                 # 版本管理：001 mobile唯一索引 / 002 删明文列(备而不发)
├── deploy/                  # APISIX 三件套(compose/config/routes) + DRILL.md 演练记录
├── scripts/                 # 网关注入/去重/清尾扫描/分层检查/离线自测
├── tests/                   # pytest 106 项（tools/config/core/data/api/model/scripts）
└── docs/ARCHITECTURE.md     # 三端协同架构文档
```

## 6. 快速开始

### 6.1 依赖与配置

```bash
python 3.13 + FastAPI + LangGraph/LangChain（核心）
新增: pyjwt · bcrypt · pydantic-settings · pytest · fakeredis · alembic

cp .env.example .env   # 填写 GLM/MySQL/Redis/Nacos/AMAP/JWT_SECRET 等
```

关键新增配置项：`JWT_SECRET` / `JWT_REFRESH_SECRET`（双密钥，强随机）、`JWT_ACCESS_TTL_MIN=30`、`JWT_REFRESH_TTL_DAYS=7`、`LLM_DAILY_TOKEN_QUOTA=50000`、`AUTH_ENABLED`、`QUOTA_ENFORCE`。

### 6.2 启动

```bash
# 1) 网关（可选但推荐；也可直连 8000）
cd deploy && docker compose up -d        # standalone 模式
# etcd 模式（已部署 APISIX）: python scripts/apisix_admin_setup.py

# 2) 后端
uvicorn main:app --host 0.0.0.0 --port 8000

# 3) 前端（独立仓库）
cd FRONT/Chatte && npm run dev           # VITE_API_BASE_URL 指向网关 :9080
```

### 6.3 一分钟验证鉴权全链路

```bash
# 登录即注册 → 双令牌
curl -X POST http://127.0.0.1:9080/user/login \
  -H 'Content-Type: application/json' \
  -d '{"mobile":"13800001111","password":"Abcd1234"}'

# 带 token 访问（user_id 一律取自令牌）
curl "http://127.0.0.1:9080/chat/agent?question=头疼怎么办&session_id=s1" \
  -H "Authorization: Bearer {access_token}"

# 30 分钟后 → 401(40102) → 续期（refresh 单次有效，轮换）
curl -X POST http://127.0.0.1:9080/user/token/refresh \
  -d '{"refresh_token":"{refresh_token}"}'

# 当日用量
curl http://127.0.0.1:9080/user/quota -H "Authorization: Bearer ..."
```

## 7. 接口一览

### 鉴权（`/user`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/user/login` | 登录即注册，颁发双令牌（响应无任何密码字段） |
| POST | `/user/token/refresh` | refresh 原子轮换（单次有效） |
| POST | `/user/logout` | 登出：access 拉黑 + refresh 删除 |
| GET | `/user/quota` | 当日 token 用量/剩余 |

### 问诊（`/chat`，均需 `Authorization: Bearer`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/chat/agent` | 多 Agent 问诊（身份取自令牌；配额前置拦截；预警话术） |
| GET | `/chat/llm` | 简单单轮聊天（调试） |
| POST | `/chat/message/save` | 消息上报（Redis 缓存 + 受控异步落库） |
| GET | `/chat/session/list` | 本人历史会话分页 |
| GET | `/chat/session/history` | 会话回放（**属主校验**，非本人 404） |
| POST | `/chat/session/close` | 关闭会话（仅属主） |

### 知识库（`/rag`，需鉴权，网关从严限速）

`/rag/save/file` · `/rag/save/web` · `/rag/delete` · `/rag/search`

> 统一响应结构 `{code, msg, data, trace_id}`；业务码：0 成功 / 40101 未登录 / 40102 过期 / 40103 无效 / 40104 已吊销 / 40105 凭据错误 / 42901 配额用尽 / 50000 系统异常。

## 8. 测试与质量

```bash
.venv/bin/python -m pytest tests/ -q            # 106 项全绿（TDD 红→绿可追溯）
.venv/bin/python scripts/check_layering.py      # 依赖方向静态检查
```

- 覆盖矩阵：JWT 全场景（篡改/过期/轮换/吊销/禁用）、密码迁移幂等、配额超限零调用断言、ast 注入样例集、会话数据隔离、兼容通道与止血开关、异步落库受控语义
- 真实环境已验证：三链路 e2e（问诊/科普/权益）、网关限流熔断演练、refresh 续期、配额真实入账（见 `deploy/DRILL.md` 与 `技改.md` §9）

## 9. 演进路线

| 阶段 | 内容 | 状态 |
|---|---|---|
| S1 | Supervisor 全局记忆（滚动窗口+折叠摘要） | ✅ e2e 验证 |
| S2 | rights 私有记忆 + 预约状态机 | ✅ e2e 验证 |
| 生产化 | JWT 双令牌 / APISIX / 配额 / 四层归位 / TDD | ✅ 24 任务交付（`openspec/changes/prod-arch-refactor`） |
| S3 | symptom / education / triage 同构私有记忆 | 待启动 |
| P5+ | SSE 流式（前端已就绪，网关参数已预置）、CI/CD、OTel 追踪 | 待启动 |
| 远期 | Temporal 预约全生命周期、检索 Rerank、用户健康画像 | 规划（`docs/ARCHITECTURE.md` §8） |

## 10. 免责声明

本项目用于多 Agent 编排技术与医疗信息分诊场景的**学习与研究演示**。系统输出的分诊建议与科普内容均不能替代执业医师诊断，紧急情况请立即拨打 120 或前往最近医院急诊科。

