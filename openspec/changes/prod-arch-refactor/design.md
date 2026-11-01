# 技术方案：YiBot 生产级四层架构技改（安全止血 + 鉴权 + 限流熔断 + Token 配额 + 分层归位）

## 上下文

**关联产物**：
- 提案：`openspec/changes/prod-arch-refactor/proposal.md`（已评审通过，前端 Chatte 纳入范围）
- 规格：`specs/security-hardening`、`specs/access-auth`、`specs/gateway-traffic-governance`、`specs/llm-token-quota`、`specs/production-hardening`（均已评审通过）
- 需求基线：《技改.md》v2.0（2026-10-31 定稿）

**技术栈现状**（本方案技术栈 = 现有栈增强，非模板默认 Java 栈）：
- 后端：Python 3.13 / FastAPI / LangGraph / SQLAlchemy（同步）/ aiomysql / redis（同步客户端）/ langchain-openai（GLM）
- 前端：React 19 + TypeScript + Vite（`/Users/jizuz/WorkSpace/FRONT/Chatte`），原生 fetch
- 基础设施：MySQL、Redis（均已部署）；APISIX + etcd 为本次新增（docker-compose）

**代码基线关键事实**（设计依据）：
- `main.py`：lifespan 已有 checkpointer 构建 + MCP 网关 start/stop 骨架，图实例挂 `app.state`；CORS `allow_origins=["*"]`；
- `database/models/user.py`：`password`（明文，String 200）与 `hash_pwd`（闲置，String 200）并存；**mobile 无唯一索引**；含 `disabled`/`is_delete` 软字段；
- `database/conn.py`：同步 engine `echo=True`、pool 8+15；`async_db_save` 裸 `threading.Thread`；checkpointer `host="localhost"` 硬编码；
- `manager/session_manager.py`：同步 `redis.Redis` 在 async 路由调用；会话键 `agent:user:{uid}:session` / `agent:session:{sid}` / `agent:context:{sid}`（TTL 900s）；
- `llm/base_llm.py`：工厂 `chat_model()` 温度 0.7 硬编码，无 timeout/retries；
- `api/user.py`：`/user/add`、`/user/detail/mobile`（返回含 password）、`/user/delete/{mobile}`，无鉴权；响应裸模型/HTTPException 混用；
- `api/chat.py:26`：`uid = int(user_id) if user_id.isdigit() else user_id`，`thread_id = user-{uid}-{sid}` 与 Checkpointer 存量数据绑定；
- `session_message` 表无幂等/去重键；`session.user_id` 为 String(64)；
- 前端：`chatte-auth` 键存 User JSON（可能含明文密码）；`API_BASE_URL` 直连 `http://118.178.184.46:8000`。

**已定架构决策**（提案 §1.3，不再讨论）：
1. MCP 工具独立工具层；2. JWT 校验在应用层（FastAPI 依赖注入），APISIX 专注流量治理；3. "token 限制" = LLM 配额（应用层）+ JWT 双令牌。

## 目标 & 非目标

✅ 技术目标
- 落地五个能力规格（安全止血/鉴权/网关/配额/生产化），接口行为与 spec 一一对应；
- 可灰度：鉴权（`AUTH_ENABLED`）、配额（`QUOTA_ENFORCE`）双开关，异常时可紧急降级并告警；
- 可回滚：网关可整体摘除恢复直连；密码迁移双读常驻；前端切流可回退（query 兼容通道）；
- 可观测：结构化日志全链路 trace_id；限流/熔断/计量/续期均有指标与告警口径；
- 零逻辑平移：编排层（graph/supervisor/子 Agent/struct）仅移动位置与装配方式，行为回归一致；
- 单测可注入：模型实例由 lifespan 构造注入，消除模块级单例。

❌ 本次不做的技术改造
- 后端 SSE 流式（仅网关参数预留：proxy_read_timeout、禁用响应缓冲）；
- CI/CD 流水线与 Docker 化交付（pytest 纳入，流水线登记 issue）；
- 双向量库收敛、RAG 检索链路改造、:8090 RAG 后台鉴权；
- LangSmith/OTel 接入（仅预留 trace_id）、RBAC、消息队列引入（无 MQ 依赖，进程内异步）。

## 方案选型与对比

| # | 决策点 | 候选与对比 | 选型（结论） |
| - | ------ | ---------- | ------------ |
| 1 | JWT 库 | A. pyjwt：轻量、社区活跃、算法白名单可控；B. python-jose：维护停滞、历史 CVE | **pyjwt**（HS256，显式 `algorithms=["HS256"]` 防算法混淆） |
| 2 | 密码哈希 | A. passlib[bcrypt]： CryptContext 自带迁移/校验抽象；B. bcrypt 裸用：无上下文管理；C. argon2：更强但引入新栈 | **passlib[bcrypt]**（ rounds=12 默认；技改已定） |
| 3 | JWT 校验位置 | A. 应用层依赖注入：与用户库联动（禁用校验/登出黑名单）天然一致；B. APISIX jwt-auth consumer：与用户表割裂、多服务下用户体系分裂 | **应用层**（已定决策 2） |
| 4 | 限流熔断 | A. APISIX 插件：配置化、语言无关、限流维度（IP/并发/计数）齐全；B. 应用内 slowapi/resilience 库：语言绑定、长连接并发语义弱、每实例独立计数 | **APISIX**（limit-concurrency + limit-req + limit-count[redis] + api-breaker） |
| 5 | token 计量 | A. 模型层全局 CallbackHandler：一处挂载覆盖全部调用点（supervisor/子 Agent/折叠/路由）；B. 各 Agent 零散实现：易漏、重复计数 | **A：全局 CallbackHandler**（`on_llm_end` 读 `token_usage`） |
| 6 | 配额存储 | A. Redis INCRBY：原子累计+天然 TTL+读检查 O(1)；B. MySQL 计数表：行锁竞争、过期清理繁琐 | **A：Redis**（键 `quota:llm:{uid}:{yyyymmdd}`，TTL 48h） |
| 7 | 异步落库 | A. 现状裸 Thread：无上限、失败静默；B. asyncio.create_task + 受控并发（信号量+重试 1 次+失败日志）；C. 引入 MQ：强可靠但新增组件 | **B**（无 MQ 依赖；at-least-once，幂等缺口登记 issue） |
| 8 | 同步 repo 处置 | A. 全量切 SQLAlchemy 2.0 async engine：改造面大、违背"零逻辑平移"；B. 保留同步 repo，经 `run_in_threadpool` 受控执行 + FastAPI 依赖统一 session 生命周期 | **B**（P4 仅归位与统一 session；async engine 另立 issue） |
| 9 | 数学求值 | A. ast 解析 + 节点白名单（仅算术）；B. 保留 eval 禁 builtins：可绕过（`__import__` 等）；C. 手写词法解析：成本高 | **A：ast 白名单**（技改已定） |
| 10 | 配置管理 | A. pydantic-settings：类型校验、单一 Settings 实例、环境变量同名兼容；B. 继续 os.getenv 散落：无校验、无单一事实源 | **A**（`config/settings.py`） |
| 11 | 前端令牌管理 | A. 引入 axios 拦截器：新增依赖；B. 原生 fetch 薄封装（authFetch：注入头/401 续期重放/单飞去重）：零新依赖 | **B** |
| 12 | 前端令牌存储 | A. 双令牌均 localStorage：简单但 XSS 双暴露；B. access 内存 + refresh localStorage（推荐默认）；C. httpOnly Cookie：最安全但需登录接口 Set-Cookie + CORS 凭据改造 | **B**（spec 已按此口径评审） |

## 架构设计

```mermaid
flowchart TB
    subgraph CLIENT["Chatte 前端 :5173"]
        UI[页面组件] --> AUTH[useAuth]
        AUTH --> AF[authFetch 封装<br/>注入 Bearer / 401 续期重放 / 单飞去重]
    end
    AF -->|"HTTP :9080（唯一入口）"| GW[APISIX 网关<br/>limit-concurrency / limit-req /<br/>limit-count(redis) / api-breaker / CORS]
    GW -->|"上游 :8000（不再外露）"| API[接入层 api/<br/>routes + deps/auth + responses]
    subgraph APP["FastAPI 应用"]
        API --> ORCH[编排层 orchestration/<br/>graph + supervisor + 4子Agent + prompts]
        ORCH --> MODEL[模型层 model/<br/>provider + token_meter callback]
        ORCH --> TOOLS[工具层 tools/<br/>mcp_gateway / tavily / custom]
        API --> DATA[数据层 data/<br/>mysql repos / redis_client / vector / ckpt]
        ORCH --> DATA
        MODEL --> DATA
        CORE[core/ 横切：security(JWT/bcrypt)<br/>quota / logging(trace_id)] -.被各层引用.-> API & ORCH & MODEL & DATA
        CFG[config/settings.py] -.-> APP
    end
    MODEL -.LLM 调用.-> LLM[GLM 开放平台]
    TOOLS -.MCP.-> MCPS[MCP Server(Nacos)]
    DATA --> MySQL[(MySQL<br/>user/session/session_message)]
    DATA --> Redis[(Redis<br/>auth:* / quota:* / agent:*)]
```

**依赖方向（单向，静态检查强制）**：`api → orchestration → model/tools → data`；`core/` 与 `config/` 被各层引用、不反向依赖业务层；工具层禁止反向依赖编排/接入层。

**目录结构改动清单**（P4 物理归位，纯平移）：

```
YiBot/
├── main.py                      # 改：仅 lifespan 组装（构建图/模型/redis/checkpointer）+ 挂载路由 + 全局 handler
├── config/settings.py           # 新：pydantic-settings 集中 30+ 散落 env
├── api/                         # 【接入层】
│   ├── deps/auth.py             # 新：get_current_user（HTTPBearer）
│   ├── deps/db.py               # 新：get_db 统一（替换各路由自建）
│   ├── routes/{chat,user,rag,auth}.py
│   └── responses.py             # 新：统一响应包装 + 全局异常 handler + 业务码
├── core/                        # 【横切】
│   ├── security.py              # 新：JWT 签发/校验、bcrypt、黑名单/白名单语义
│   ├── quota.py                 # 新：配额检查/入账/查询
│   └── logging.py               # 新：结构化日志 + trace_id（contextvar）
├── orchestration/               # 【编排层】= agent/ 平移（graph/supervisor/sub_agent/struct）
│   └── prompts/                 # 新：6 处硬编码 prompt 集中（内容不变，版本化命名）
├── model/                       # 【模型层】= llm/ 扩展
│   ├── provider.py              # chat_model 工厂 + timeout/max_retries/temperature 可配
│   └── callbacks/token_meter.py # 新：全局 token 计量
├── tools/                       # 【工具层】mcp_gateway 等原样保留；mcp_tool.py 改 AMAP_KEY 走配置；custom_tools.py 改 ast
├── data/                        # 【数据层】
│   ├── mysql/{conn,models,user_repo,session_repo}.py   # session 上下文统一、异常上抛、echo=False
│   ├── redis_client.py          # 新：redis.asyncio 单例（替代 manager/ 内同步客户端）
│   ├── vectorstore/             # 本地 Chroma 相关平移
│   └── checkpointer.py          # aiomysql 抽离，host/port 走配置
├── schemas/                     # API DTO（与 orchestration/struct/schemas.py 划界：API 出入参 vs LLM 输出）
├── scripts/                     # test_mcp.py 移入；新增清尾扫描脚本
└── deploy/
    ├── docker-compose.yml       # apisix + etcd
    └── apisix/{config.yaml, routes.yaml}
```

同步清理：空文件 `agent/checkpointer.py`、空目录 `utils/`；`manager/session_manager.py` 并入 `data/` + `api/` 职责拆分（会话缓存逻辑归 `data/redis_client.py` 生态，对外接口逻辑归 routes）。

**装配方式**：模块级 `model = chat_model()` 全部消除——`main.py` lifespan 内构造 `{settings, redis, checkpointer, graph, model}` 挂 `app.state`，路由经 `request.app.state` 或依赖注入获取，支持单测替换实例。

**前端 Chatte 改造结构**：
- `src/api/http.ts`（新）：`authFetch()` —— 注入 `Authorization`；401 且有 refresh 时单飞续期（模块级 Promise 锁）→ 重放原请求 → 续期失败抛 `UnauthorizedError`；
- `src/api/auth.ts`（新）：`login/refresh/logout/fetchQuota`；
- `src/hooks/useAuth.ts`（改）：登录走 `/user/login`；access 保存于内存模块（`src/api/tokenStore.ts`），refresh 存 localStorage 新键 `chatte-refresh`；启动时若有 refresh 则静默续期恢复；登出调用服务端 + 清理 `chatte-auth`（旧键，防明文残留）与 `chatte-refresh`；
- `src/api/{chat,session,message}.ts`（改）：全部改走 `authFetch`，移除 user_id query/body 传参（`fetchChatReply`/`streamChatReply`/`fetchSessionList`/`closeSession`/`fetchSessionHistory`/`saveChatMessage`）；
- `src/api/user.ts`（删）：`loginOrRegister`/`queryUserByMobile`/`createUser` 伪登录链路整体移除；
- `src/config/index.ts`（改）：`API_BASE_URL` 默认值切网关地址，`api.user` 增 login/refresh/logout/quota。

## 存储设计

### MySQL 表变更

**user 表**（复用现有列，最小改动）：

| 列 | 变更 | 说明 |
| -- | ---- | ---- |
| `hash_pwd` String(200) | 启用 | 存 bcrypt 哈希（`$2b$12$...`，60 字符，200 列宽够用） |
| `password` String(200) | 过渡期保留 → 最终删除 | 明文列；新写一律置空字符串；清尾验证全空后经 Alembic 删除（独立迁移，P5 末） |
| `mobile` | **新增唯一索引 `uk_user_mobile`** | 支撑"登录即注册"按手机号查询与并发防重（现状无索引，且存在重复风险；迁移脚本先去重再建索引） |
| `disabled` | 读入 | 登录/get_current_user 校验 `disabled=0`（复用现有列，不新增） |

新增表：**无**（令牌无状态 + Redis 白名单，不落库）。

**Alembic 引入**（P5）：
- `alembic baseline`：对齐现有 `user` / `session` / `session_message`（及 checkpointer 三表，标记为 managed-externally 不纳管版本）；
- 迁移 001：`uk_user_mobile` 唯一索引（前置数据去重：同手机号保留 `gmt_create` 最早且未删除的记录，其余标记 `is_delete=1` 并告警清单输出）；
- 迁移 002（清尾完成后的独立变更窗口）：`ALTER TABLE user DROP COLUMN password`。
- 约束：**thread_id/checkpointer 存量表零变更**；`session.user_id` 存量值为字符串形态的 uid，格式不变。

### Redis 缓存设计

redis.asyncio 单例（连接池 max 20），全部键带 TTL，禁止裸键：

| Key | 值 | TTL | 用途 | 更新策略 |
| --- | --- | --- | --- | --- |
| `auth:refresh:{jti}` | `{uid}` | 7d（=refresh 有效期） | refresh 白名单；轮换时 DEL 旧 jti、SET 新 jti | 登录/续期时写；登出/轮换时删 |
| `auth:deny:{jti}` | `1` | =access 剩余有效期（≤30min） | access 黑名单（登出吊销） | 登出时写；到期自动清除 |
| `quota:llm:{uid}:{yyyymmdd}` | int（token 数） | 48h | 每用户每日用量累计 | `INCRBY` 原子累加；入口检查 `GET` |
| `agent:user:{uid}:session` 等 3 类 | 现状不变 | 900s | 会话态（存量逻辑平移，键格式不变） | 现状逻辑 |

防护要点：
- 无缓存穿透面（配额键不存在即视为 0，不缓存空值对象）；黑/白名单键短 TTL 自然过期，无雪崩聚集（jti 分散）；
- 续期并发去重：refresh 以"旧 jti 是否仍在白名单"为幂等依据——`GETDEL auth:refresh:{old_jti}` 原子取出，成功者签发新令牌对，并发失败方收到 401 走重新登录（满足 spec"仅一次生效"）；
- 配额"先检查后入账"存在窗口超放（≤1 轮对话），spec 已声明容差，不做分布式锁（避免长对话持锁）。

### ES/其他存储（按需）
- 不涉及。向量库现状保持（Chroma 本地目录 + MCP 侧 Qdrant），仅目录归位。

## 接口契约

**统一响应结构**（`api/responses.py`）：`{"code": 0, "msg": "ok", "data": {...}, "trace_id": "..."}`；HTTP 状态码与业务码并用（网关层用 HTTP 429/503，应用层业务码细分）。

**业务码段**：`0` 成功；`40100-40199` 鉴权（40101 未登录 / 40102 令牌过期 / 40103 令牌无效 / 40104 已吊销 / 40105 凭据错误）；`42900-42999` 配额（42901 当日额度用尽）；`50000` 系统异常。

| 接口 | 方法 | 鉴权 | 入参 | 出参（data） | 说明 |
| ---- | ---- | ---- | ---- | ------------ | ---- |
| `/user/login` | POST | 否 | `{mobile, password}` | `{access_token, refresh_token, token_type:"bearer", expires_in, user:{uid,name,mobile,role}}` | 登录即注册；响应无任何密码字段 |
| `/user/token/refresh` | POST | 否 | `{refresh_token}` | 同上（新令牌对，refresh 轮换，旧 refresh 立即作废） | 40103=旧 refresh 已轮换/吊销 |
| `/user/logout` | POST | Bearer | - | `null` | access jti 入黑名单 + DEL refresh jti |
| `/user/quota` | GET | Bearer | - | `{date, used, quota, remaining}` | 当日用量 |
| `/chat/agent` 等全部 `/chat/*` | GET/POST | Bearer（过渡期兼容 query user_id + 告警日志） | 同现状（user_id 参数标记 deprecated） | 同现状 + 统一包装 | 身份以令牌 uid 为准，query user_id 一律忽略 |
| `/rag/*`（主服务路由） | - | Bearer | 同上 | 同上 | 管理类，网关限速从严 |
| `/user/detail/mobile`、`/user/add` | GET/POST | 过渡期：脱敏（响应剔除 password/hash_pwd）+ Bearer；前端切换验收后**下线**（路由移除） | - | - | 旧伪登录链路 |

**JWT 令牌结构**：

```
access:  {uid, mobile, role, type:"access",  jti, exp(now+30min), iat}   # HS256, JWT_SECRET
refresh: {uid, type:"refresh", jti, exp(now+7d), iat}                    # HS256, JWT_REFRESH_SECRET（独立密钥，防令牌类型混用）
```

`get_current_user` 校验链：签名 → `exp` → `type=="access"` → Redis 黑名单 `auth:deny:{jti}` 不存在 → `disabled==0` → 取 `uid`（int 化逻辑与 `api/chat.py:26` 现状一致）。

**前端行为契约**：全部业务请求经 `authFetch`；401（40102）→ 单飞 refresh → 重放原请求一次 → 仍失败则清理态并跳登录；40103/40104 → 直接清理态跳登录。SSE 请求（`streamChatReply`）同样带 Authorization，EventSource 不可用时保持现有 fetch 流式读取实现。

## 异步流程设计（进程内异步，无 MQ）

| 流程 | 触发 | 实现 | 失败策略 | 幂等/顺序 |
| ---- | ---- | ---- | -------- | --------- |
| token 计量入账 | `on_llm_end`（全局 CallbackHandler） | `asyncio.create_task(quota.record(uid, total_tokens))`，信号量并发≤10 | 失败记 ERROR 日志（含 trace_id），不重试不影响主流程 | INCRBY 天然幂等累加；乱序可容忍 |
| 会话/消息落库 | 现各 `@async_db_save` 调用点 | 改 `asyncio.create_task` → `run_in_threadpool(sync_repo_fn)`，模块级队列上限 1000（满则告警+丢弃计数） | 重试 1 次；仍失败 ERROR 日志（trace_id + 业务键） | at-least-once；`session_message` 无唯一键，重复容忍（回放重复行影响可忽略，登记 issue） |
| 密码清尾扫描 | 每日 03:00 进程内 asyncio 定时 | `SELECT count(*) WHERE password != ''` | 只读告警，不改数 | 只读天然幂等 |
| 密码惰性升级 | 存量用户登录成功 | 登录事务内：写 `hash_pwd` + 置空 `password` | 失败则本次按明文成功返回 + WARN 日志（下次重试） | 条件更新 `WHERE mobile=? AND (hash_pwd IS NULL OR hash_pwd='')` 防并发双写 |

## 分布式事务 & 一致性策略

- **无跨服务事务**（单库 + Redis），不引入 Seata/本地消息表类机制；
- **登录流程**：查用户（不存在则建用户，单库事务）→ bcrypt 校验 → 写 refresh 白名单（Redis）→ 返回令牌。白名单写失败时降级：access 正常颁发（30 分钟可用），refresh 不可用（7 天长效受损），返回体标记 `refresh_granted:false` 并 WARN——避免登录主流程被 Redis 抖动阻断；用户重新登录即自愈；
- **登出**：黑名单写失败时重试 1 次，仍失败仅清前端态并 ERROR 告警（旧 access 最长 30 分钟自然过期，风险窗口有界）；
- **配额**：检查（读）与入账（写）异步解耦，容忍 ≤1 轮超放（spec 已声明）；48h TTL 内 `INCRBY 累计值` vs `日志 token_usage 汇总` 抽样对账（对账脚本，diff>5% 告警）；
- **Redis 与 MySQL 会话双态**：维持现状"Redis 为运行态、MySQL 为持久态"，不引入补偿（登记 issue：Redis 故障时会话创建不可用的降级预案）。

## 限流、熔断、降级（APISIX，非 Sentinel）

**路由 × 插件配置**（`deploy/apisix/routes.yaml`，阈值全部可配）：

| 路由 | 插件 | 关键参数 |
| ---- | ---- | -------- |
| `/chat/agent` | limit-concurrency + limit-req + api-breaker | 并发 50（key=remote_addr）；rate=2 burst=5；breaker: 失败 3 次→熔断 10s→半开 |
| `/chat/*` 其余 | limit-req + api-breaker | rate=10/s |
| `/user/login`、`/user/token/refresh` | limit-count | 20/min per IP，`policy: redis`（跨网关实例共享计数，复用现有 Redis） |
| `/rag/*` | limit-req | rate=5/s |
| 全局 | cors（白名单收敛：`http://localhost:5173` 与生产前端域名）、proxy-rewrite 预留 | SSE 预留：`proxy_read_timeout 300s` + 关闭响应缓冲（注释模板，P5 后启用） |

**降级链**（三层独立）：
1. 网关层：限流/熔断响应体带友好文案与 trace_id（`{"code":42910,"msg":"当前咨询人数较多，请稍后再试"}`）；
2. 鉴权层：`AUTH_ENABLED=false` 紧急放行（跳过 get_current_user，身份回退 query user_id 兼容通道）——仅限事故止血，触发时 P0 告警；
3. 配额层：`QUOTA_ENFORCE=false` 仅计量不拦截（观察模式），配额数据继续累计。

## 数据迁移 & 灰度策略

**存量数据脚本**（`scripts/`）：
1. `migrate_dedup_mobile.py`：mobile 去重（保留最早未删记录，其余 `is_delete=1`，输出清单人工复核）→ 前置于 Alembic 001；
2. `migrate_hash_pwd_report.py`：扫描统计明文存量（`password != ''`）基线，供清尾比对；
3. 清尾扫描脚本（每日定时）：明文残留数 >0 告警。

**密码迁移（不停机三步）**：
- 步骤 1（P1 起）：新注册只写 `hash_pwd`、`password=''`；
- 步骤 2（持续）：存量登录成功即惰性升级（条件更新，见异步流程表）；
- 步骤 3（清尾告警归零 + 观测一个周期后）：Alembic 002 删列。

**灰度与切流节奏**：
1. P0 止血上线（Key 轮换已完成 → 代码出库）；
2. P1 鉴权双端联调：后端 `AUTH_ENABLED=true` + query 兼容通道 → 前端 Chatte 切新登录（本地/测试环境验证 DevTools 无 user_id、无明文）→ 生产发布；
3. P2 网关并行期：APISIX 部署并与 8000 直连并存（压测验证限流/熔断）→ 前端 `API_BASE_URL` 切 9080 → 观测无误后 8000 关闭外网暴露（云安全组）；
4. P3 配额：`QUOTA_ENFORCE=false` 观察模式跑 1~2 天（核对计量准确性）→ 开启拦截；
5. 观测窗口：query 兼容告警量归零且持续一个发布周期 → 移除兼容通道与旧接口（独立小变更）。

**回退通道**：前端 `VITE_API_BASE_URL` 环境变量可即时切回直连；网关整体摘除 = 流量回 8000；鉴权/配额开关配置热生效。

## 风险点、降级、回滚方案

> 重点：必须明确回滚路径，上线前确认可回滚

| 风险 | 影响等级 | 降级方案 | 回滚方式 |
| ---- | -------- | -------- | -------- |
| 前端切换窗口期错配（旧前端 + 新后端 / 反之） | 高 | 后端 query user_id 兼容通道 + 告警日志；前端旧键清理逻辑 | 回滚前端发布（旧版仍走伪登录？否——旧接口已脱敏致旧前端不可用，故**后端必须先于前端上线**且保留兼容通道；回滚即前端回旧版本 + 后端 AUTH_ENABLED=false） |
| 存量明文密码迁移未完成即删列 | 高 | 删列 DDL 前置条件：清尾告警归零 + 观测一个周期 | Alembic 002 down() 恢复列；迁移期间 hash_pwd 写入永不删除 |
| APISIX 对长连接/流式缓冲（P5 后暴露） | 中 | 网关参数已预留（read_timeout/禁缓冲）；limit-concurrency 对长连接占位语义压测确认 | 网关整体摘除，流量回直连 8000 |
| 配额误伤重度用户（长对话单轮数万 token） | 中 | `QUOTA_ENFORCE=false` 观察模式；配额值可配；预警话术 80% | 开关关闭即时生效（仅计量不拦截） |
| 计量漏计（模型未返回 token_usage / 异常路径） | 中 | 容差口径 spec 已声明；48h 抽样对账 diff>5% 告警 | 无需回滚（只影响统计精度，不影响拦截安全性） |
| 目录大迁移与业务改动冲突 | 中 | P4 放最后、纯平移、迁移前后三链路回归 | git revert 迁移提交（无逻辑改动，回滚安全） |
| Redis 故障（白名单/配额/会话全依赖） | 高 | 登录降级（access 可用 refresh 不可用）；会话维持现状风险（登记 issue） | Redis 恢复后自愈；AUTH_ENABLED=false 应急 |
| mobile 去重误判（存量重复数据） | 低 | 去重脚本输出清单人工复核后才执行 Alembic 001 | `is_delete` 软标记可逆恢复 |
| 前端令牌 XSS 窃取 | 中 | access 仅内存 + 30min 短时效；refresh 单用途轮换（窃取窗口小） | 吊销通道：用户重新登录即轮换全部 jti |

## 监控、日志、告警

- **日志**：`core/logging.py` 结构化 JSON（时间/级别/模块/trace_id/uid/事件），contextvar 贯穿请求→编排→模型→数据；替换全部 print（grep 验证零残留）；密码/令牌/密钥字段一律不落日志（脱敏中间件统一过滤）；
- **trace_id**：中间件生成（或透传前端 `X-Request-Id`），随统一响应体返回，网关限流/熔断响应亦携带；
- **关键指标与告警阈值**：

| 指标 | 告警阈值 | 说明 |
| ---- | -------- | ---- |
| 401 拒绝率 / 续期失败率 | 续期失败率 >10%（5min 窗口） | 前端异常或密钥配置错误 |
| query user_id 兼容告警量 | 突增（前端回退信号）/ 用于下线判定 | 归零一个发布周期后下线 |
| 网关限流触发次数 / 熔断事件 | 熔断打开即告警 | 流量异常或后端故障 |
| 配额拦截次数 | 环比突增 3 倍 | 误伤信号 |
| 计量入账失败次数 | >0 累积告警 | Redis 抖动 |
| 异步落库队列丢弃计数 | >0 告警 | 背压信号 |
| 清尾扫描明文残留数 | >0（清尾期后） | 迁移缺口 |

- 告警接收：日志平台关键字订阅（本次不建独立告警系统，登记 issue：接入统一告警通道）。

## 测试方案

- **单元测试**（pytest，`tests/` 按层组织）：
  - `core/security`：签发/校验/过期/篡改/type 混用/黑名单/白名单轮换（内存 fakeredis）；
  - `tools/custom_tools`：ast 白名单——注入样例集（`__import__`、`os.system`、lambda、逗号表达式、超长表达式）全部拒绝；合法算术全通过；
  - `core/quota`：检查/入账/剩余计算、跨日重置（fakeredis TTL 模拟）；
  - `api/responses`：统一包装/异常兜底/业务码映射；
  - 前端：`authFetch` 续期重放/单飞去重（vitest 或最小手测脚本，视前端测试基建）。
- **集成测试**（FastAPI TestClient + fakeredis + sqlite/test MySQL）：
  - 鉴权全链路：登录（新号=注册）→ 带令牌访问 → access 过期 401 → refresh 续期 → logout 后双令牌拒绝；
  - 兼容通道：无令牌 + query user_id 放行且产生告警日志；`AUTH_ENABLED=false` 放行；
  - 配额拦截：调低配额 → 下一轮 429 业务码 + 不进图（mock graph 断言零调用）；
  - 存量明文登录升级：升级后 password 列为空、二次登录走哈希。
- **e2e 回归**：三条业务链路（问诊/科普/权益）人工 + 脚本回归，行为与迁移前一致（对话输出采样比对）；
- **压测**：wrk 打 `/chat/agent`（mock 模型延迟 5s）验证并发 51 触发限流；登录接口 21 次/分触发限流；停后端验证熔断开启→冷却→半开恢复；
- **对账校验**：48h 窗口内 `quota` 键累计 vs 日志 token_usage 汇总，diff>5% 告警。

## CR评审检查清单（blocker / 建议项，直接用于PR评审）

🔴 阻断项（不满足不能合并）
- **安全红线**：任何新增代码不得出现硬编码密钥/密码；日志与响应不得输出 password/hash_pwd/令牌/密钥；禁止 `eval`/`exec`/`pickle.loads`/`yaml.load`（非 safe）/SQL 字符串拼接（必须参数化）；
- **鉴权完整性**：`/chat/*`、`/rag/*`、新接口必须接入 `get_current_user`；身份只取令牌 uid，query/body user_id 仅允许出现在显式兼容通道内且必须带告警日志；`get_current_user` 校验链（签名/exp/type/黑名单/disabled）不得缺项；
- **令牌语义**：refresh 必须白名单校验且轮换（旧 jti 原子作废）；登出 access 即刻黑名单；两密钥分离不得共用；
- **数据幂等与一致性**：密码升级条件更新（防并发双写）；mobile 唯一索引前置去重完成才可合并建索引迁移；`thread_id` 格式与 uid 数值转换逻辑零改动；
- **配额前置**：拦截必须在 `graph.ainvoke` 之前（断言超限时模型零调用）；计量 callback 必须挂全局（新增模型调用点自动覆盖）；
- **异步与资源**：禁止在 async 路由内直接调用阻塞客户端（redis/同步 IO 必须经 async 客户端或 threadpool）；新 Redis 键必须带 TTL；后台任务必须有失败日志与上限；
- **网关与回滚**：限流/熔断阈值必须走配置文件不硬编码；`deploy/` 配置与 routes 一致可部署；鉴权/配额开关真实可用（回滚路径验证）；
- **平移纪律**：orchestration 平移 diff 中不得混入逻辑变更（prompt 文案、路由条件、状态机改动一律拒绝，拆分变更）；
- **响应规范**：新接口一律统一响应结构；异常不得吞错（禁止 `except: pass` / 异常时返回空集合）。

🟡 优化建议项（不阻塞合并，登记issue后续迭代）
- CI/CD 流水线与 Docker 化交付（技改 P5 原含，本次以 pytest 覆盖替代）；
- `session_message` 幂等键（业务键 + 时间窗去重）；
- SQLAlchemy 2.0 async engine 全量切换；
- Redis 会话降级预案（Redis 故障时直查 MySQL 恢复会话创建）；
- 登录风控增强（验证码/设备指纹）、按账号维度限流；
- 前端令牌 httpOnly Cookie 方案、refresh 自动定时预续期；
- LangSmith/OTel 接入、统一告警通道；
- prompt 版本化管理工具（快照 diff）；
- 双向量库收敛方案、OTel trace 与 MCP stats 打通。

## 上线步骤

1. **配置/脚本先行**：高德控制台 Key 轮换并验证旧 Key 失效 → 生产 `.env` 新增 JWT/配额/AMAP 配置项（Settings 兼容旧变量名，可先空跑）→ 存量数据扫描脚本出基线报告；
2. **P0 止血**：代码出库（AMAP_KEY 走配置）+ ast 替换 eval + `vector_db/` 出 git → 部署验证三工具正常；
3. **P1 鉴权（后端先上）**：后端发布（AUTH_ENABLED=true + 兼容通道 + 旧接口脱敏）→ 观察 curl 全链路 → 前端 Chatte 发布（新登录 + authFetch + base 不变仍直连）→ 双端联调验收（DevTools 检查项过）；
4. **P2 网关**：APISIX+etcd 部署并行期 → 压测/熔断演练 → 前端 base 切 9080 → 观测 → 8000 关闭外网；
5. **P3 配额**：观察模式 1~2 天 → 对账核对 → 开启拦截；
6. **P4+P5 归位与生产化**：目录平移 + 回归三链路 + Alembic（001 唯一索引）+ 清尾扫描启动；
7. **上线后观测窗口**：兼容告警归零一个发布周期 → 下线兼容通道与旧接口（小变更）；清尾归零一个周期 → Alembic 002 删明文列。

## 方案备注

- **排期对照**：P0 0.5 天 / P1 2~3 天（含前端） / P2 1~2 天 / P3 1~2 天 / P4 2~3 天 / P5 1~2 周（技改 §6）；P2 可与 P1 并行；
- **跨仓库执行**：本变更 tasks 含 YiBot（后端）与 Chatte（前端）两仓库任务，apply 阶段分别提交、分别 PR，CR 清单共用；
- **上游对齐声明**：本方案全部默认口径继承自已评审的 proposal/spec（过渡期一个发布周期、SSE 仅预留、access 内存 + refresh 持久化、旧接口脱敏后下线、配额 50k/日）；若后续发现与上游冲突，按流程退回上游重新对齐，不在本层私自抹平；
- **协同**：高德 Key 轮换（运维）、生产前端域名备案/白名单（部署）、云安全组 8000 收敛（运维）需提前预约窗口。



