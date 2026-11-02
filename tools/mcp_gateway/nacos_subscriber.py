"""Nacos 订阅式服务发现：后台协程周期拉取健康实例列表，维护本地快照并支持变更 diff 回调。

说明：Python 生态缺少官方 Nacos 2.x gRPC 订阅 SDK，这里以「短周期轮询(默认 10s±20% 抖动) + 变更 diff 回调」
实现订阅语义，拉取失败时保留上一次快照(Nacos 抖动不清空实例，死实例由上层熔断兜底)，
未来可无缝替换为 gRPC 订阅而不影响上层接口。
"""
import asyncio
import logging
import random
import time
from typing import Any, Awaitable, Callable

import httpx

from tools.mcp_gateway.config import (
    MCP_SERVICE_NAME,
    MCP_TIMEOUT,
    NACOS_GROUP,
    NACOS_NAMESPACE,
    NACOS_PASSWORD,
    NACOS_SERVER_ADDR,
    NACOS_TOKEN_TTL,
    NACOS_USERNAME,
)

logger = logging.getLogger("yibot.mcp.nacos")

# 变更回调: async fn(added: list[dict], removed: list[dict])
ChangeCallback = Callable[[list[dict], list[dict]], Awaitable[None]]


def nacos_base_url() -> str:
    """规范化 Nacos 服务端地址，统一带协议前缀且不带末尾斜杠。"""
    addr = NACOS_SERVER_ADDR.strip().rstrip("/")
    if not addr.startswith(("http://", "https://")):
        addr = f"http://{addr}"
    return addr


def instance_key(inst: dict) -> str:
    """实例唯一标识：优先 Nacos instanceId，兜底 ip:port。"""
    return str(inst.get("instanceId") or f"{inst.get('ip')}:{inst.get('port')}")


def extract_healthy_instances(payload: Any) -> list[dict]:
    """从 Nacos 响应提取健康实例，兼容 v1(hosts) 与 v2(data / data.list) 两种结构。"""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            candidates = data.get("list") or []
        elif isinstance(data, list):
            candidates = data
        else:
            candidates = payload.get("hosts") or []
    else:
        candidates = []

    return [
        inst for inst in candidates
        if isinstance(inst, dict)
        and inst.get("healthy") is True
        and inst.get("ip")
        and inst.get("port")
    ]


class NacosSubscriber:
    """后台订阅器：周期拉取 MCP Server 健康实例，维护快照 + 变更回调。"""

    def __init__(self, interval: float | None = None, on_change: ChangeCallback | None = None):
        self._interval = interval if interval and interval > 0 else 10.0
        self._on_change = on_change
        self._instances: list[dict] = []
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stopped = asyncio.Event()
        self._refresh_lock = asyncio.Lock()
        # 鉴权 token 缓存
        self._token: str | None = None
        self._token_expire_at: float = 0.0
        # 观测状态
        self._last_success_at: float | None = None
        self._last_error: str | None = None
        self._consecutive_errors = 0
        self._refresh_count = 0

    # ==================== 生命周期 ====================

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name="mcp-nacos-subscriber")
        logger.info("Nacos 订阅已启动(service=%s, group=%s, interval=%.1fs)",
                    MCP_SERVICE_NAME, NACOS_GROUP, self._interval)

    async def stop(self) -> None:
        self._stopped.set()
        self._wake.set()
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 停止阶段吞掉一切
                pass
        logger.info("Nacos 订阅已停止")

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 单轮失败不终止订阅
                self._consecutive_errors += 1
                self._last_error = str(e)
                logger.warning("Nacos 订阅轮询失败(连续 %d 次): %s", self._consecutive_errors, e)
            # 等待：正常 interval±20% 抖动；被 refresh_soon() 提前唤醒则立即重查
            jitter = self._interval * random.uniform(0.8, 1.2)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=jitter)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def refresh_soon(self) -> None:
        """提示订阅器尽快刷新(如调用失败怀疑实例已死)，不阻塞调用方。"""
        self._wake.set()

    # ==================== 鉴权 ====================

    async def _access_token(self) -> str | None:
        """Nacos 开启鉴权时登录换取 accessToken(带 TTL 缓存)；未配置账号返回 None。"""
        if not NACOS_USERNAME:
            return None
        now = time.monotonic()
        if self._token and now < self._token_expire_at:
            return self._token
        try:
            async with httpx.AsyncClient(timeout=MCP_TIMEOUT) as client:
                resp = await client.post(
                    f"{nacos_base_url()}/nacos/v1/auth/login",
                    data={"username": NACOS_USERNAME, "password": NACOS_PASSWORD},
                )
                resp.raise_for_status()
                self._token = (resp.json() or {}).get("accessToken")
                self._token_expire_at = now + NACOS_TOKEN_TTL
                return self._token or None
        except Exception as e:  # noqa: BLE001
            logger.warning("Nacos 登录失败，尝试匿名访问: %s", e)
            return None

    # ==================== 拉取与快照 ====================

    async def fetch_instances(self) -> list[dict]:
        """单次拉取健康实例列表：v1/v2 接口依次尝试，抛出异常由调用方处理。"""
        params: dict[str, Any] = {
            "serviceName": MCP_SERVICE_NAME,
            "groupName": NACOS_GROUP,
            "healthyOnly": "true",
        }
        if NACOS_NAMESPACE:
            params["namespaceId"] = NACOS_NAMESPACE
        token = await self._access_token()
        if token:
            params["accessToken"] = token

        errors: list[str] = []
        base = nacos_base_url()
        async with httpx.AsyncClient(timeout=MCP_TIMEOUT) as client:
            for api_path in ("/nacos/v1/ns/instance/list", "/nacos/v2/ns/instance/list"):
                try:
                    resp = await client.get(f"{base}{api_path}", params=params)
                    if resp.status_code != 200:
                        errors.append(f"{api_path} -> HTTP {resp.status_code}: {resp.text[:120]}")
                        continue
                    instances = extract_healthy_instances(resp.json())
                    if not instances:
                        errors.append(f"{api_path} -> 无健康实例: {resp.text[:120]}")
                        continue
                    return instances
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{api_path} -> {e}")
        raise RuntimeError("Nacos 拉取失败: " + "; ".join(errors))

    async def refresh_once(self) -> list[dict]:
        """刷新一轮：拉取 -> diff -> 更新快照 -> 触发变更回调(串行化，避免并发拉取)。"""
        async with self._refresh_lock:
            fetched = await self.fetch_instances()
            self._refresh_count += 1
            self._consecutive_errors = 0
            self._last_error = None
            self._last_success_at = time.time()

            old_map = {instance_key(i): i for i in self._instances}
            new_map = {instance_key(i): i for i in fetched}
            added = [inst for k, inst in new_map.items() if k not in old_map]
            removed = [inst for k, inst in old_map.items() if k not in new_map]

            self._instances = list(fetched)
            logger.info("Nacos 实例快照: 共 %d 个健康实例%s", len(fetched),
                        f"(+{len(added)} -{len(removed)})" if (added or removed) else "")
            if added:
                logger.info("实例上线: %s", [f"{i['ip']}:{i['port']}" for i in added])
            if removed:
                logger.info("实例下线: %s", [f"{i['ip']}:{i['port']}" for i in removed])

        if (added or removed) and self._on_change is not None:
            try:
                await self._on_change(added, removed)
            except Exception as e:  # noqa: BLE001 回调失败不影响订阅
                logger.warning("实例变更回调执行失败: %s", e)
        return self._instances

    def get_instances(self) -> list[dict]:
        """当前快照(浅拷贝，防止外部修改)。"""
        return list(self._instances)

    def status(self) -> dict:
        return {
            "running": self.running,
            "instances": [f"{i.get('ip')}:{i.get('port')}" for i in self._instances],
            "refresh_count": self._refresh_count,
            "last_success_at": self._last_success_at,
            "consecutive_errors": self._consecutive_errors,
            "last_error": self._last_error,
        }
