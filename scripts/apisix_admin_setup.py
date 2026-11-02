"""APISIX Admin API 配置注入（T15：etcd 模式网关适配）。

将 deploy/apisix/routes.yaml 的路由/插件规则经 Admin API 写入
（适配 etcd 模式部署：/Users/jizuz/WorkSpace/Docker/apisix-docker/example）。

用法：.venv/bin/python scripts/apisix_admin_setup.py [--base http://127.0.0.1:9180]
"""

import argparse
import json
import os
import urllib.request

ADMIN_KEY = os.environ.get(
    "APISIX_ADMIN_KEY", "edd1c9f034335f136f87ad84b625c8f1"
)
UPSTREAM_HOST = os.environ.get("APISIX_UPSTREAM_HOST", "host.docker.internal")
REDIS_HOST = os.environ.get("APISIX_REDIS_HOST", "host.docker.internal")
REDIS_PASSWORD = os.environ.get("APISIX_REDIS_PASSWORD", "")
# limit-count redis policy 公共配置（密码为空时不传字段——null 会导致 schema 校验 400）
REDIS_POLICY = {
    "policy": "redis",
    "redis_host": REDIS_HOST,
    "redis_port": 6379,
    **({"redis_password": REDIS_PASSWORD} if REDIS_PASSWORD else {}),
}


def _limit_count(msg: str) -> dict:
    return {
        "count": 20, "time_window": 60,
        "key_type": "var", "key": "remote_addr",
        **REDIS_POLICY,
        "rejected_code": 429,
        "rejected_msg": msg,
    }


# limit-concurrency 不在 APISIX 默认插件列表：镜像未显式启用前默认关闭
# （启用方式：apisix_conf/config.yaml 增补 plugins 段后重启容器，再设此环境变量）
ENABLE_LIMIT_CONCURRENCY = os.environ.get("APISIX_ENABLE_LIMIT_CONCURRENCY") == "1"

BREAKER = {
    "break_response_code": 503,
    "unhealthy": {"http_statuses": [500, 502, 503, 504], "failures": 3},
    "healthy": {"successes": 2},
    "timeout": 10,
}


def _put(base: str, path: str, payload: dict) -> str:
    request = urllib.request.Request(
        f"{base}/apisix/admin/{path}",
        data=json.dumps(payload).encode("utf-8"),
        method="PUT",
        headers={
            "X-API-KEY": ADMIN_KEY,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"❌ PUT {path} -> {exc.code}: {body}") from exc
    return result.get("action") or result.get("key", path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:9180")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    # ---- 上游：宿主 YiBot:8000 ----
    result = _put(base, "upstreams/yibot_backend", {
        "type": "roundrobin",
        "nodes": {f"{UPSTREAM_HOST}:8000": 1},
        "checks": {
            "active": {
                "type": "http", "http_path": "/", "timeout": 3,
                "healthy": {"interval": 3, "successes": 2},
                "unhealthy": {"interval": 2, "failures": 3},
            }
        },
    })
    print(result, "upstreams/yibot_backend")

    # ---- 全局 CORS（design：白名单收敛至网关） ----
    result = _put(base, "global_rules/cors_global", {
        "plugins": {
            "cors": {
                "allow_origins": "http://localhost:5173,http://127.0.0.1:5173",
                "allow_methods": "GET,POST,PUT,DELETE,OPTIONS",
                "allow_headers": "Authorization,Content-Type,X-Request-Id",
                "expose_headers": "X-Request-Id",
                "max_age": 3600,
            }
        },
    })
    print(result, "global_rules/cors_global")

    routes = {
        # 问诊对话：限速为主 + 熔断（并发上限待镜像启用 limit-concurrency 后叠加）
        "chat_agent": {
            "uri": "/chat/agent",
            "upstream_id": "yibot_backend",
            "plugins": {
                **({
                    "limit-concurrency": {
                        "concurrency": 50, "key": "remote_addr",
                        "rejected_code": 429,
                        "rejected_msg": "当前咨询人数较多，请稍后再试",
                    },
                } if ENABLE_LIMIT_CONCURRENCY else {}),
                "limit-req": {
                    "rate": 2, "burst": 5, "key_type": "var",
                    "key": "remote_addr", "rejected_code": 429,
                },
                "api-breaker": BREAKER,
            },
        },
        # 其余对话/会话类
        "chat_rest": {
            "uri": "/chat/*",
            "upstream_id": "yibot_backend",
            "plugins": {
                "limit-req": {
                    "rate": 10, "burst": 20, "key_type": "var",
                    "key": "remote_addr", "rejected_code": 429,
                },
                "api-breaker": BREAKER,
            },
        },
        # 登录/续期：按 IP 计数限流（Redis 跨实例共享）
        "user_login": {
            "uri": "/user/login",
            "upstream_id": "yibot_backend",
            "plugins": {
                "limit-count": _limit_count("操作过于频繁，请稍后再试"),
            },
        },
        "user_refresh": {
            "uri": "/user/token/refresh",
            "upstream_id": "yibot_backend",
            "plugins": {
                "limit-count": _limit_count("操作过于频繁，请稍后再试"),
            },
        },
        # 用户/配额
        "user_rest": {
            "uri": "/user/*",
            "upstream_id": "yibot_backend",
            "plugins": {
                "limit-req": {
                    "rate": 10, "burst": 20, "key_type": "var",
                    "key": "remote_addr", "rejected_code": 429,
                },
            },
        },
        # 知识库从严
        "rag_all": {
            "uri": "/rag/*",
            "upstream_id": "yibot_backend",
            "plugins": {
                "limit-req": {
                    "rate": 5, "burst": 10, "key_type": "var",
                    "key": "remote_addr", "rejected_code": 429,
                },
            },
        },
    }

    for route_id, payload in routes.items():
        result = _put(base, f"routes/{route_id}", payload)
        print(result, f"routes/{route_id} ({payload['uri']})")

    print("✓ 网关配置注入完成（阈值与 deploy/apisix/routes.yaml 一致）")


if __name__ == "__main__":
    main()
