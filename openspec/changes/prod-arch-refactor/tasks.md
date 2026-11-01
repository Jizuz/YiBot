# 开发任务清单

> 变更：`prod-arch-refactor`（YiBot 后端 + Chatte 前端双仓库）
> 基于 Proposal / Specs / Design 评审结论生成；任务粒度=单 PR，小步合并；
> **TDD 强制**：每个任务单元先写失败测试（红）→ 确认失败原因正确 → 最小实现（绿）→ 重构；`[测试]` 前缀任务必须先于对应 `[实现]` 任务勾选。

## 任务列表

| 序号 | 任务描述 | 关联spec | 优先级 | 预估工时 | 交付物 | 依赖任务 |
| ---- | -------- | -------- | ------ | -------- | ------ | -------- |
| T1 | ✅[测试+实现] 数学表达式安全求值：先写注入样例失败测试（eval 现状下 `__import__` 等样例），再以 ast 白名单替换 `tools/custom_tools.py:27` 的 eval | security-hardening | P0 | 2h | `tests/tools/test_custom_tools.py` + 修复代码 | — |
| T2 | ✅[实现] 高德 Key 出库：`tools/mcp_tool.py:35` 改读配置 `AMAP_KEY`；全库扫描硬编码密钥/密码（grep 清单归档） | security-hardening | P0 | 1h | 修复代码 + 扫描记录 | 运维前置：Key 已轮换 |
| T3 | ✅[实现] 工程卫生：`vector_db/` 出 git（.gitignore + rm --cached）；删空文件 `agent/checkpointer.py`、空目录 `utils/`；`test_mcp.py` → `scripts/` | security-hardening | P0 | 1h | git 状态清理提交 | — |
| T4 | ✅[测试+实现] 配置集中：`config/settings.py`（pydantic-settings，兼容旧变量名，含 JWT/配额/AMAP/模型参数）；测试先覆盖加载/默认值/缺失报错 | production-hardening | P0 | 3h | `config/settings.py` + `tests/config/test_settings.py` | — |
| T5 | ✅[测试+实现] 横切底座：`core/logging.py`（结构化 JSON + trace_id contextvar + 脱敏过滤）+ trace 中间件；`api/responses.py` 统一响应/全局异常 handler + 业务码 | production-hardening | P1 | 4h | core/logging + api/responses + 单测 | T4 |
| T6 | ✅[测试+实现] Redis 异步客户端：`data/redis_client.py`（redis.asyncio 单例，lifespan 装配 app.state）；fakeredis 单测连接生命周期 | production-hardening | P1 | 2h | data/redis_client.py + 单测 | T4 |
| T7 | ✅[测试+实现] JWT 核心：`core/security.py`——双令牌签发/校验链（签名/exp/type/黑名单/disabled）、bcrypt、refresh 白名单原子轮换（GETDEL）、独立双密钥 | access-auth | P1 | 5h | core/security.py + `tests/core/test_security.py`（全场景矩阵） | T4, T6 |
| T8 | ✅[测试+实现] 密码迁移：user_repository 哈希读改 + 登录惰性升级（条件更新防并发双写）+ 新注册只写哈希 | security-hardening | P1 | 3h | repo 改造 + `tests/data/test_password_migration.py` | T7 |
| T9 | ✅[测试+实现] 鉴权接口：`api/routes/auth.py`——login（登录即注册/凭据错误不区分存在性/Redis 白名单写失败降级）、refresh 轮换、logout 吊销；旧接口 `/user/detail/mobile`、`/user/add` 脱敏（响应剔除 password/hash_pwd） | access-auth | P1 | 4h | routes/auth.py + TestClient 集成测试 | T7, T8, T5 |
| T10 | ✅[测试+实现] 路由接入鉴权：`api/deps/auth.py` get_current_user；`/chat/*`、`/rag/*` 全量注入；user_id 改取令牌（保留 uid 数值转换与 thread_id 格式）；query 兼容通道（放行+告警日志）；`AUTH_ENABLED` 开关 | access-auth | P1 | 4h | deps/auth.py + 路由改造 + 兼容通道测试 | T9 |
| T11 | ✅[测试+实现] 前端请求层：Chatte `src/api/http.ts` authFetch（Bearer 注入 / 401 单飞续期 / 重放一次 / 失败登出）+ `tokenStore.ts`（access 内存 + refresh localStorage `chatte-refresh`）+ `src/api/auth.ts` 四接口 | access-auth | P1 | 4h | 前端 api 层 + 续期重放测试 | T9（联调可后置） |
| T12 | ✅[实现] 前端登录态迁移：`useAuth.ts` 改走 /user/login（响应无密码）；启动静默续期；登出调服务端 + 清理旧键 `chatte-auth`；删除 `api/user.ts` 伪登录链路；`config/index.ts` api 地址更新 | access-auth | P1 | 3h | 前端登录流改造 | T11 |
| T13 | ✅[实现] 前端去 user_id：`chat.ts / session.ts / message.ts` 全部改走 authFetch 并移除 user_id 传参（含 SSE 请求带 Authorization）；DevTools 验证清单执行 | access-auth | P1 | 2h | 前端请求改造 + 验证记录 | T11, T12 |
| T14 | ✅[实现] 网关部署：`deploy/docker-compose.yml`（apisix+etcd）+ `config.yaml` + `routes.yaml`（limit-concurrency/limit-req/limit-count[redis]/api-breaker/cors 白名单/SSE 参数预留注释） | gateway-traffic-governance | P2 | 4h | deploy/ 全套配置 | T4（限流阈值可配） |
| T15 | [验证] 网关演练：wrk 压测触发并发/速率限流；停后端验证熔断→冷却→半开；登录 21 次/分触发限流；前端 base 切 9080 + 8000 外网收敛操作手册 | gateway-traffic-governance | P2 | 3h | 演练记录 + 切流手册 | T14, T10 |
| T16 | ✅[测试+实现] token 计量：`model/callbacks/token_meter.py` 全局 CallbackHandler（on_llm_end 解析 token_usage，异步入账失败不影响主流程）；覆盖 supervisor/子 Agent/折叠/路由全部调用点验证 | llm-token-quota | P2 | 3h | token_meter + 单测 | T6 |
| T17 | ✅[测试+实现] 配额核心：`core/quota.py`——检查/入账（INCRBY）/查询/剩余预警阈值；`quota:llm:{uid}:{date}` TTL 48h；fakeredis 全场景（累计/跨日/超限/边界） | llm-token-quota | P2 | 3h | core/quota.py + `tests/core/test_quota.py` | T16, T7 |
| T18 | ✅[测试+实现] 配额接入：`/chat/agent` 入口前置拦截（graph.ainvoke 之前，超限 429 业务码+话术不进图）+ `QUOTA_ENFORCE` 观察模式开关 + `GET /user/quota` 接口 + 预警话术 | llm-token-quota | P2 | 3h | 拦截逻辑 + quota 接口 + mock graph 零调用断言 | T17 |
| T19 | [实现] 编排归位：`agent/` → `orchestration/` 纯平移（graph/supervisor/sub_agent/struct）+ `orchestration/prompts/` 集中 6 处 prompt（内容不变）+ 模块级模型单例改 lifespan 注入；平移后三链路回归 | production-hardening | P3 | 4h | orchestration/ + 回归记录 | T10, T18（业务功能全就绪后） |
| T20 | [实现] 数据层归位：`database/+manager/+rag/` → `data/`；repo 统一 session 上下文（contextmanager）+ 异常上抛不吞错 + `echo=False`；checkpointer host/port 配置化；`async_db_save` 裸线程改受控 asyncio 任务（队列上限+失败日志） | production-hardening | P3 | 4h | data/ + 回归记录 | T19 |
| T21 | [实现] 模型层归位：`llm/` → `model/provider.py`（timeout/max_retries/temperature 可配）+ `graph.ainvoke` 补 recursion_limit + 依赖方向静态检查脚本（无反向 import）+ 全量 print 清零 grep | production-hardening | P3 | 3h | model/ + 检查脚本 | T19, T20 |
| T22 | [实现] Alembic 引入：baseline 对齐三表现状 + `migrate_dedup_mobile.py` 去重脚本（清单人工复核）+ 迁移 001 `uk_user_mobile`；002 删列脚本**只准备不执行** | security-hardening | P3 | 3h | alembic 环境 + 脚本 | T8 |
| T23 | [实现] 清尾扫描：`scripts/migrate_hash_pwd_report.py` 基线报告 + 每日定时只读扫描告警 | security-hardening | P3 | 2h | 扫描脚本 + 基线报告 | T22 |
| T24 | [验证] 全量回归与回写：三链路 e2e（问诊/科普/权益）+ 鉴权/网关/配额验收信号全过 + `技改.md` 回写落地差异 | 全部 | P3 | 3h | 回归报告 + 文档回写 | T19~T23 |

### 任务细分说明

> 每个任务单元严格按 **红（写失败测试并确认失败原因正确）→ 绿（最小实现）→ 重构** 推进；下方"要点"中的测试项即红阶段交付。

- 任务T1（ast 安全求值）：
  - 要点：红——注入样例集测试（`__import__("os")`、`os.system`、lambda、逗号表达式、`e**99999`、超长串）在现状 eval 下暴露执行风险或断言缺失；绿——`ast.parse` + 节点白名单（仅 `Expression/BinOp/UnaryOp/Constant(num)/Pow`，深度与长度上限）；
  - 自测要点：合法算术（四则/括号/小数/负号）全部正确；注入样例全部返回参数错误且**零求值**。
- 任务T4（Settings）：红——缺失必填项抛错、默认值、旧变量名兼容读取；绿——pydantic-settings 单例；**本任务不改动现有读取点**（平移在 T19~T21）。
- 任务T5（日志/响应）：红——统一包装结构断言、未知异常兜底 50000、trace_id 透传与回显、密码字段脱敏过滤；绿——中间件 + handler。
- 任务T7（JWT 核心，矩阵最全）：红→绿场景：签发/校验通过；exp 过期；篡改 payload；access/refresh type 混用（双密钥交叉校验必须失败）；黑名单命中；refresh GETDEL 轮换后旧 jti 失败；disabled=1 拒绝。
- 任务T8（密码迁移）：红——明文存量首次登录后 `hash_pwd` 有值且 `password=''`；二次登录走哈希；并发双登录仅一次升级（条件更新断言）；绿——repo 改造。
- 任务T9/T10（接口/路由）：TestClient + fakeredis 集成：登录即注册、凭据错误不区分账号存在性、refresh 轮换、logout 后双令牌拒绝、无 token 401、query 伪造 user_id 无效、兼容通道放行且产生告警日志、`AUTH_ENABLED=false` 放行；**thread_id 与 uid int 转换快照断言不变**。
- 任务T11~T13（前端）：红（若有测试基建）——authFetch 401 触发单飞续期仅一次、重放携带新令牌、续期失败清理态；无基建则以手测脚本 + DevTools 清单替代（记录于验证记录）；user_id 移除后 DevTools Network 零 `user_id` 参数、零明文密码。
- 任务T16~T18（配额）：红——on_llm_end 解析 `token_usage`（含缺失字段容错）、入账失败主流程不中断（异常吞噬断言改为日志断言）；超限时 **mock graph 调用次数为 0**；观察模式不拦截但计量继续；绿——按 design 键设计实现。
- 任务T19~T21（归位）：**平移纪律**——diff 审查仅允许 import 路径/模块位置变化，任何逻辑/prompt 文案/路由条件改动拆分独立变更（CR 阻断项）；每步迁移后跑既有 pytest + 三链路冒烟。
- 任务T22/T23（迁移）：去重脚本 dry-run 输出清单人工复核后才执行；002 删列脚本入库但**禁止执行**（前置条件：清尾告警归零 + 观测一个周期）。

## 单元测试要求

- 必须覆盖的场景：正向（登录→访问→续期→登出全链路）、分支（登录即注册/惰性升级/兼容通道/观察模式）、异常（伪造/过期/吊销/Redis 失败降级/计量失败）、幂等（refresh 轮换/密码条件更新/重复升级）、边界（配额恰好用尽/跨日 0 点/表达式极限值）；
- Mock 依赖方式：Redis 一律 fakeredis；模型/graph 以注入实例替换（T19 前用 monkeypatch 模块属性，T19 后经 app.state 注入）；时间敏感用例（exp/TTL）freezegun 或注入时钟；HTTP 层用 FastAPI TestClient；前端无既有测试基建，authFetch 以最小手测脚本覆盖（登记 issue：vitest 基建）；
- 每个任务勾选前必须附测试运行输出（红→绿各一次）。

## 预检查清单（提CR前自检）

- [ ] 单元测试补充完成（红→绿记录可追溯）
- [ ] CR 阻断清单（design.md §CR评审检查清单）逐项自查，特别是：无硬编码密钥/密码、无 print 新增、无 eval/exec/SQL 拼接、新 Redis 键带 TTL
- [ ] 日志（含 trace_id）、埋点、异常处理补齐；日志无敏感字段
- [ ] 配置项同步（.env.example 与 settings 一致）、限流/熔断阈值走配置
- [ ] 数据库变更脚本（Alembic/去重）单独评审，不与业务代码混 PR
- [ ] 平移类任务 diff 审查：零逻辑变更
- [ ] pytest 全绿；三链路冒烟通过

## 上线准备清单

- [ ] 高德 Key 轮换完成且旧 Key 已作废（运维确认）
- [ ] 数据库脚本评审（去重清单人工复核、001 索引、002 备而不发）
- [ ] 生产 .env 配置变更提交（JWT 双密钥/配额/AMAP/AUTH_ENABLED/QUOTA_ENFORCE）
- [ ] 回滚路径演练：网关摘除、AUTH 开关、QUOTA 开关、前端 base 回切
- [ ] 告警规则配置（design §监控告警 7 项指标关键字订阅）
- [ ] 双端联调验收：DevTools 检查项（无 user_id、无明文密码、Authorization 头、401 自动续期）全过
- [ ] 三链路 e2e 回归完成 + 压测/熔断演练记录归档

## 任务备注

- **跨仓库提交**：T1~T10、T14~T24 在 YiBot 仓库；T11~T13 在 Chatte 仓库（`/Users/jizuz/WorkSpace/FRONT/Chatte`）；两仓库分别 PR，共用本清单与 CR 阻断清单；
- **顺序约束**：T2 依赖运维完成 Key 轮换（先轮换后出库）；T19~T21（归位）必须在 T10/T18（鉴权/配额业务功能）全绿后执行，避免业务改动与目录迁移叠加；T13 完成前 T10 的兼容通道必须保留；
- **外部协同**：生产前端域名/CORS 白名单、云安全组 8000 收敛需预约运维窗口（T15）；
- **登记 issue（不阻塞）**：CI/CD 流水线、Docker 化、session_message 幂等键、async engine、Redis 会话降级、httpOnly Cookie、OTel、vitest 基建（清单见 design 🟡 项）；
- **排期对照**：P0=T1~T4（0.5~1 天）、P1=T5~T13（2~3 天，T11~T13 可与 T14 并行）、P2=T14~T18（2~3 天）、P3=T19~T24（1~2 周，含回归），与技改 §6 一致。

