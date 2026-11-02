# T15 网关演练记录（2026-11-02）

环境：APISIX 3.19.0-debian（etcd 模式，`/Users/jizuz/WorkSpace/Docker/apisix-docker/example`）
后端：YiBot uvicorn 0.0.0.0:8000（宿主）；上游 `host.docker.internal:8000`
配置注入：`APISIX_REDIS_PASSWORD=<宿主Redis密码> .venv/bin/python scripts/apisix_admin_setup.py`

## 演练结果（全部通过）

| # | 场景 | 阈值 | 实测 | 结论 |
|---|------|------|------|------|
| ① | 经网关登录（登录即注册） | - | 200 + 双令牌 + `x-request-id` 透传 | ✅ |
| ② | CORS 白名单 | 5173 白名单 | 白名单 Origin 获授权头；恶意 Origin 无授权 | ✅ |
| ③ | 登录限流（limit-count/Redis 共享） | 20/min/IP | 第 21 次 **429**（计数含演练首登，精确） | ✅ |
| ④ | 速率限流（/chat/agent，limit-req） | rate=2 | 60 并发 → **54×429**（仅 6 个漏出穿透） | ✅ |
| ⑤ | 熔断 api-breaker | 3 失败→10s→半开 | 3×502 → 熔断 **503 快速失败 32ms** → 重启后端 10s 后半开探测恢复（401/200） | ✅ |

## 已知语义与差异（回写）

1. **limit-req 为 per-worker 本地漏桶**：多 worker 容器下聚合阈值 = rate × worker 数
   （60 并发打 /chat/* rate=10 未触发即此因）。聚合精确限流用 limit-count（Redis 共享，
   ③ 已验证）；阈值标定需按 worker 数折算。
2. **limit-concurrency 不在默认插件列表**：需在 `apisix_conf/config.yaml` 增补
   `plugins: [limit-concurrency, ...]` 并重启容器后，以
   `APISIX_ENABLE_LIMIT_CONCURRENCY=1` 重跑注入脚本叠加并发上限（50）。
3. **Redis 密码必传**：宿主 Redis 开启 requirepass 时插件未传密码报
   `failed to limit count`（经 `APISIX_REDIS_PASSWORD` 注入解决）。
4. 演练中 6 个穿透请求后端返回 500（旧进程日志已覆盖，非网关行为）——
   纳入 T24 三链路 e2e 回归观察项。

## 复现命令

```bash
# 注入配置（含限流/熔断/CORS，阈值与 deploy/apisix/routes.yaml 一致）
cd YiBot && APISIX_REDIS_PASSWORD=*** .venv/bin/python scripts/apisix_admin_setup.py

# 登录限流验证：第 21 次 429
for i in $(seq 1 21); do curl -s -o /dev/null -w "%{http_code} " \
  -X POST http://127.0.0.1:9080/user/login -H 'Content-Type: application/json' \
  -d '{"mobile":"13800002222","password":"Drill456"}'; done

# 熔断验证：停后端 → 3×502 → 503 快速失败 → 重启 10s 后半开恢复
```
