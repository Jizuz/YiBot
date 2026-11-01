# 提案：YiBot 生产级四层架构技改（安全止血 + 鉴权 + 限流熔断 + Token 配额 + 分层归位）

## 背景动因

1. **安全事件驱动（P0，不可推迟）**：高德 API Key 硬编码于 `tools/mcp_tool.py:35` 且已提交 git 历史（已泄漏，必须先在控制台轮换、再改代码出库）；`tools/custom_tools.py:27` 使用 `eval()` 执行数学表达式，存在代码注入漏洞；用户密码明文入库（`hash_pwd` 字段闲置）。
2. **越权与凭据暴露**：`api/chat.py:18` 的 user_id 由 query 明文传入、全站无鉴权，任何人可冒充任意用户查看会话、查询权益；前端 Chatte 伪登录依赖 `GET /user/detail/mobile`（响应含明文密码）并在浏览器本地比对密码——密码对抓包/前端完全可见。
3. **稳定性与成本失控**：LLM 长耗时调用（单次图调用可 30s+）无限流熔断保护；主链路 LLM 调用完全无 token 计量，成本无配额约束。
4. **生产化缺失**：CORS 全开（`main.py:42`）、print 日志、响应格式三套并存、无登录接口（前端 localStorage 伪登录）、配置散落 30+ 处 `os.getenv`。
5. **为什么是现在**：业务（医疗问诊）上线在即，上述能力是上线门槛；Key 泄漏每拖一天风险放大一次；后续目录归位放最后可避免与业务改动叠加冲突（技改已定序 P0→P5）。

> 需求来源：《技改.md》（v2.0 定稿，2026-10-31 基线）——本提案为其 OPSX 落地版本，技术细节以 design 阶段展开。

## 当前现状

- **技术栈**：Python 3.13 / FastAPI / LangGraph，约 3400 行；Supervisor + 4 子 Agent（分诊/症状/科普/权益）医疗问诊编排。
- **目录已隐含分层雏形**：`api/`（chat/user/rag）、`agent/`（supervisor + 子 Agent + struct）、`llm/`（15 行工厂）、`database/ + manager/ + rag/`、`tools/`，但命名与依赖未收敛（目标四层：接入/编排/模型/数据 + 独立工具层）。
- **编排层是全项目最成熟部分**：硬路由状态标记、跨请求预约状态机、分层记忆、全链路结构化输出均已就绪——本次以平移为主、零逻辑改动。
- **已实施**：`tools/mcp_gateway/` 进程级 MCP 客户端网关（订阅式 Nacos 发现、熔断、负载均衡、工具收敛）。
- **关键问题**（按层）：
  - 接入层：无鉴权、无统一响应/异常、print 日志、CORS 全开、`/chat/agent` 阻塞式无超时、无登录接口；
  - 编排层：prompt 硬编码散落 6 文件、模块级单例无法 mock、`graph.ainvoke` 未设 `recursion_limit`、无追踪；
  - 模型层：温度硬编码、无 timeout/max_retries、token 计量仅存在于死代码（未挂载路由的 agent）；
  - 工具层：高德 Key 泄漏、`eval()` 注入；
  - 数据层：裸 `threading.Thread` 落库（无上限/失败静默丢消息）、同步 redis 阻塞事件循环、repo 自开自关 session 且吞错、双向量库并存（本地 Chroma + MCP 侧 Qdrant）、`vector_db/` 二进制入库、无 Alembic。
- **存量数据约束**：`thread_id = user-{uid}-{sid}` 与 Checkpointer 存量数据绑定，格式与 uid 数值类型转换逻辑（`api/chat.py:26`）不可变更；存量用户密码为明文，需不停机双读迁移。
- **前端 Chatte**（`/Users/jizuz/WorkSpace/FRONT/Chatte`，React 19 + TypeScript + Vite，原生 fetch 无请求层封装）：
  - 伪登录链路：`GET /user/detail/mobile`（响应含明文密码，前端本地比对）→ 用户不存在则 `POST /user/add` 明文注册；登录态存 localStorage（键 `chatte-auth`）；
  - `src/api/` 下 user / chat / session / message 四个模块全部以 query / body 明文传 `user_id`；
  - `API_BASE_URL` 默认直连生产 `http://118.178.184.46:8000`（未过网关）；RAG 知识管理后台走独立地址（dev 经 Vite 代理至 `127.0.0.1:8090`）；
  - **SSE 客户端已就绪**（`Accept: text/event-stream`，非流式响应自动回退整体解析）——后端 `/chat/agent` 流式化后前端无需大改；
  - 登出仅清 localStorage，无服务端吊销。

## 目标与非目标

✅ 本次目标（可验证、本次必须落地）

- G1【分层归位】按接入/编排/模型/数据四层 + 独立工具层完成目录归位，依赖方向单向（`api → orchestration → model/tools → data`），三条业务链路（问诊/科普/权益）e2e 回归通过；
- G2【JWT 双令牌鉴权（前后端全链路）】后端：登录 → 带 token 访问 → 过期 401 → refresh 续期全链路走通，`/chat/*`、`/rag/*` 的 user_id 一律改从 token 获取；前端 Chatte：接入真实登录、请求层统一注入 Authorization、401 自动续期、登出服务端吊销、移除 user_id 明文传参与本地密码比对；
- G3【APISIX 限流熔断】压测触发限流；停后端触发熔断并半开恢复；CORS 收敛至网关、8000 端口不再外露；
- G4【LLM token 配额】全链路 LLM 调用（supervisor/子 Agent/记忆折叠/路由）计量入 Redis；超配额前置拦截返回 429 业务码；
- G5【安全止血】高德 Key 轮换并出库、`eval()` 改 ast 安全解析、`vector_db/` 出 git、密码 bcrypt 迁移（不停机）。

❌ 非目标（明确本次不做，控制范围蔓延，后续迭代）

- 双向量库收敛（Chroma / Qdrant 职责重叠）——检索链路本次不动，仅归位目录，收敛方案另立议题；
- RAG 检索效果与召回逻辑改造；
- 编排层业务逻辑改动（含 prompt 文案变更，仅做集中管理与版本化）；
- 前端 UI / 交互 / 视觉重构（Chatte 仅做登录态与请求层的最小必要改造）；
- RBAC / 多角色权限体系（JWT payload 仅携带现有 role 字段）；
- LangSmith / OTel 全链路追踪接入（仅预留日志 trace_id，接入另立变更）；
- 压测调优、生产容量规划（仅验证限流熔断功能正确性）。

## 能力清单

### 新增能力

- `security-hardening`：存量安全止血——密钥出库（先轮换后改码）、数学表达式安全求值、向量库产物出 git、密码哈希不停机迁移；
- `access-auth`：接入鉴权（前后端全链路）——手机号+密码登录（登录即注册）、access/refresh 双令牌颁发与轮换、登出吊销、受保护接口身份从令牌获取；前端 Chatte 登录态迁移（令牌管理、请求注入、失效自动续期、移除明文 user_id 与本地密码比对）；
- `gateway-traffic-governance`：网关流量治理——分层限流（并发/速率/计数）、后端异常熔断与半开恢复、CORS 统一收敛、后端端口收敛；
- `llm-token-quota`：LLM 用量配额——全链路 token 计量、按用户按日配额、超限前置拦截与友好提示、当日用量查询；
- `production-hardening`：生产化底座——统一响应与异常规范、结构化日志（trace_id）、模型调用超时重试可配、编排递归上限、配置集中管理、目录分层归位与工程卫生清理（为审计与 CI 打底）。

### 下线移除能力

- `legacy-plain-auth`（临时标识）：伪登录全链路——前端 localStorage 伪登录（`/user/detail/mobile` 明文密码比对 + `/user/add` 明文注册）与后端 query 明文 user_id 传参——过渡期保留兼容并打告警日志，前端切换验收后下线（下线时机见"待确认问题"）。

## 纳入范围 / 排除范围

### 纳入范围

- 前端 Chatte（`/Users/jizuz/WorkSpace/FRONT/Chatte`）：`useAuth` / `loginOrRegister` 改造为调用 `POST /user/login`（响应不含密码）；请求层统一封装注入 `Authorization: Bearer`；access 过期 401 → 自动 refresh → 重试 / 失败登出；登出调用 `POST /user/logout`；移除 user / chat / session / message 各模块 user_id 明文传参；`API_BASE_URL` 切换至网关地址；localStorage 旧登录态（`chatte-auth`，含明文密码残留）迁移清理；配额余量展示（`GET /user/quota`，轻量）；
- 接口：`POST /user/login`、`POST /user/token/refresh`、`POST /user/logout`、`GET /user/quota` 新增；`/chat/*`、`/rag/*` 鉴权改造（user_id 取自 token，过渡期兼容 query + 告警）；`/user/detail/mobile` 停止返回明文密码（处置方式见"待确认问题"）；
- 数据与迁移：存量明文密码 → bcrypt 不停机迁移（登录时惰性升级 + 清尾校验）；Alembic 引入管理 user / session / session_message 表；thread_id / Checkpointer 存量格式保持不变；
- 基础设施与部署：`deploy/` 下 APISIX + etcd docker-compose、路由/插件/熔断配置、CORS 收敛；
- 配置变更：新增 `JWT_SECRET`、`JWT_REFRESH_SECRET`、`JWT_ACCESS_TTL_MIN`、`JWT_REFRESH_TTL_DAYS`、`LLM_DAILY_TOKEN_QUOTA`、`AMAP_KEY` 等并集中至 pydantic-settings；
- 告警与可观测：过渡期 user_id 兼容告警日志、配额剩余量预警、结构化日志 trace_id、token 计量与 MCP 网关 stats 可观测；
- 工程卫生：`vector_db/` 出 git、空文件/空目录清理、`test_mcp.py` 归位 scripts/、pytest 单测与回归、目录分层归位（`agent/→orchestration/`、`llm/→model/`、`database/+manager/+rag/→data/` 等）。

### 排除范围

- Chatte 前端 UI / 交互 / 视觉重构（仅登录态与请求层最小改造）；
- RAG 知识管理后台独立服务（:8090）与前端 RAG 管理页的鉴权改造（本次仅覆盖 YiBot 主服务路由，见"待确认问题"）；
- 双向量库收敛、RAG 召回优化、LangSmith/OTel 接入、RBAC（见"非目标"）；
- 生产环境容量规划与压测调参（默认阈值可配即可）；
- 历史密码/数据的一次性强刷脚本（采用登录惰性迁移，清尾任务仅告警不改数）。

## 依赖项

- **外部运维（前置）**：高德开放平台 Key 轮换（P0 第一步，先轮换再改代码）；旧 Key 失效验证；
- **前端 Chatte**（`/Users/jizuz/WorkSpace/FRONT/Chatte`，本变更内直接改造，跨仓库执行）：令牌注入、401 自动续期、移除 user_id 传参、接入 login/refresh/logout、`API_BASE_URL` 切换至网关地址；
- **基础设施**：APISIX 3.x + etcd（docker-compose 新增）、Redis（已有，切 `redis.asyncio`）、MySQL（已有）；
- **新增 Python 依赖**：pyjwt、passlib[bcrypt]、pydantic-settings；
- **存量数据**：Checkpointer thread_id 格式绑定、明文密码双读窗口、`vector_db/` 从 git 追踪移除（保留本地文件）。

## 待确认问题

1. **交付切分**：本变更是否一次性覆盖 P0~P5 全量（技改估 2~4 周），还是按里程碑拆分为多个 OPSX 变更分批交付（如 P0+P1 先行）？本提案默认全量纳入、tasks 内按 P0→P5 串行拆解。
2. **过渡期窗口**：前端已纳入本变更（双端同批交付），后端 query 传 user_id 的兼容期可缩短——建议保留一个发布周期作为回退通道后下线，是否接受？
3. **配额默认值**：`LLM_DAILY_TOKEN_QUOTA=50000`/用户/日是否确认？超配额提示话术与"剩余量预警"阈值（建议 80%）由谁提供（产品/运营）？
4. **SSE 流式**（技改 P5 项）：前端 SSE 客户端已就绪（含非流式回退），仅后端 `/chat/agent` 需流式化——是否纳入本次交付，还是仅做网关参数预留（proxy_read_timeout、禁用缓冲）？
5. **登录即注册**：手机号不存在自动注册的策略是否保留风控限制（如验证码）？技改对齐现状为无验证码，默认沿用。
6. **令牌前端存储策略**：双令牌均存 localStorage（实现简单，XSS 下可被窃取）vs access 内存 + refresh localStorage（推荐，刷新页时静默续期）vs httpOnly Cookie（最安全，但登录接口需改造为 Set-Cookie 且跨端口/域名需配套 CORS 凭据策略）——默认建议 access 内存 + refresh localStorage。
7. **旧接口与 RAG 后台处置**：`/user/detail/mobile`、`/user/add` 是随伪登录下线直接移除，还是脱敏 + 鉴权后保留？RAG 知识管理后台独立服务（:8090）是否纳入本次鉴权范围（默认不纳入）？

## 初步风险预判

- **前端切换联调**：伪登录 → 真登录、API base 切网关存在双端窗口期错配风险（后端过渡期兼容 + 告警日志、localStorage 旧键清理缓解）；
- **令牌存储安全**：localStorage 存令牌在 XSS 下可被窃取（access 短 TTL + 内存存储缓解，见"待确认问题"）；
- **数据迁移**：明文密码迁移窗口期需双读，清尾前存在存量明文残留（迁移脚本失败静默风险由清尾告警兜底）；
- **网关与长连接**：APISIX 对 SSE 长连接的缓冲/超时问题将在流式化时暴露（需提前配置；limit-concurrency 对长连接的占位语义需压测确认）；
- **配额误伤**：长对话单轮可消耗数万 token，默认 50k/日可能误伤重度用户（配额可配 + 预警话术 + 管理端可调缓解）；
- **大范围目录迁移**：与业务改动叠加冲突（技改已定 P4 物理归位放最后、零逻辑平移规避）；
- **LLM 计量准确性**：`token_usage` 依赖模型返回，流式/异常路径可能漏计（后置累计失败不影响主流程，容差需明确）。

## 验收信号

- **安全（G5）**：git 历史复查无有效密钥（旧 Key 在高德控制台已失效）；`eval()` 消除（ast 解析单测覆盖注入样例）；新注册用户仅存 bcrypt 哈希；`git ls-files` 不含 `vector_db/`；
- **鉴权（G2）**：curl 全链路——登录 → 带 token 访问 → access 过期返回 401 → refresh 续期 → logout 后旧 token 拒绝；无 token 访问 `/chat/*` 被拒；user_id 伪造（改 query）无效；前端 Chatte——登录走 `/user/login` 且响应与 localStorage 均无密码字段、请求带 Authorization 头且无 user_id 参数、access 过期自动续期不打断会话、登出后旧令牌不可用；
- **网关（G3）**：wrk 压测触发限流（429/503 限流响应）；停后端触发熔断 → 半开探测恢复；CORS 仅网关生效、8000 端口外部不可直连；
- **配额（G4）**：Redis `quota:llm:{uid}:{date}` 计数随对话增长；调低配额验证超限拦截（429 业务码 + 友好话术，不进图）；`GET /user/quota` 返回当日用量/剩余；
- **归位与生产化（G1）**：三条业务链路（问诊/科普/权益）e2e 回归通过；依赖方向静态检查（无反向 import）；pytest 通过；日志含 trace_id 无 print 残留；`graph.ainvoke` 带 `recursion_limit`；
- **回滚**：APISIX 可整体摘除（直连 8000 恢复）；鉴权开关/配额开关可配置关闭快速止血。
