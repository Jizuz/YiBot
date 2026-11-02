"""MCP 网关门面：Nacos 订阅 → 实例注册表(熔断/LB) → 工具收敛(缓存) → 校验 → 调用重试(技改 §4.4)。"""
import asyncio
import logging

from pydantic import ValidationError

from tools.mcp_gateway.client import McpRemoteTool, SseMcpClient, build_sse_url, tool_result_text
from tools.mcp_gateway.config import (
    MCP_CALL_RETRIES,
    MCP_CIRCUIT_COOLDOWN,
    MCP_CIRCUIT_FAIL_THRESHOLD,
    MCP_LB_POLICY,
    MCP_SUBSCRIBE_INTERVAL,
    MCP_TOOL_CACHE_TTL,
)
from tools.mcp_gateway.instance_registry import InstanceRegistry, InstanceState
from tools.mcp_gateway.nacos_subscriber import NacosSubscriber
from tools.mcp_gateway.tool_registry import ToolRegistry

logger = logging.getLogger("yibot.mcp.gateway")


# ==================== 异常定义 ====================

class McpGatewayError(Exception):
    """MCP 网关基础异常。"""


class McpToolNotFoundError(McpGatewayError):
    """工具在收敛注册表中不存在(本地拦截，未发起网络调用)。"""


class McpValidationError(McpGatewayError):
    """工具参数未通过本地 schema 校验(本地拦截，未发起网络调用)。"""


class McpNoInstanceError(McpGatewayError):
    """无可用 MCP 实例(未发现健康实例或全部熔断)。"""


class McpCallError(McpGatewayError):
    """重试耗尽后调用仍失败。"""


class MCPGateway:
    """MCP 客户端网关(进程内单例，见模块底部 mcp_gateway)。

    职责链: Nacos 订阅(实例快照) -> 实例注册表(熔断+LB) -> 工具收敛(缓存+去重)
            -> 调用(参数本地校验 -> 选实例 -> 建连 callTool -> 失败换实例重试)。
    """

    def __init__(self):
        self.registry = InstanceRegistry(
            policy=MCP_LB_POLICY,
            fail_threshold=MCP_CIRCUIT_FAIL_THRESHOLD,
            cooldown=MCP_CIRCUIT_COOLDOWN,
        )
        self.subscriber = NacosSubscriber(
            interval=MCP_SUBSCRIBE_INTERVAL,
            on_change=self._on_instances_changed,
        )
        self.tools = ToolRegistry(ttl=MCP_TOOL_CACHE_TTL)
        self._started = False
        self._load_lock = asyncio.Lock()
        self._stats = {
            "call_total": 0, "call_success": 0, "call_failure": 0,
            "validate_blocked": 0, "retry_total": 0,
        }

    # ==================== 生命周期 ====================

    async def start(self) -> None:
        """启动订阅并同步实例；任何失败只记日志不阻塞(降级为后续懒加载)。"""
        if self._started:
            return
        self._started = True
        await self.subscriber.start()
        try:
            await self.subscriber.refresh_once()
        except Exception as e:  # noqa: BLE001
            logger.warning("网关启动时首次实例拉取失败(将由订阅循环重试): %s", e)
        self.registry.sync_from_nacos(self.subscriber.get_instances())
        logger.info("MCP 网关已启动: 实例 %d 个, LB=%s, 熔断阈值=%d/冷却=%.0fs",
                    len(self.subscriber.get_instances()), self.registry.policy,
                    self.registry.fail_threshold, self.registry.cooldown)
        asyncio.create_task(self._safe_warm_tools(), name="mcp-gateway-warm")

    async def stop(self) -> None:
        await self.subscriber.stop()
        self._started = False
        logger.info("MCP 网关已停止")

    async def _safe_warm_tools(self) -> None:
        try:
            await self._load_tools()
        except Exception as e:  # noqa: BLE001
            logger.warning("工具缓存预热失败(首次访问时重试): %s", e)

    async def _on_instances_changed(self, added: list[dict], removed: list[dict]) -> None:
        """订阅回调: 对齐注册表；以注册表差集判断真实变更 -> 工具缓存失效(下轮重拉收敛)。

        注意: 不能直接用订阅器的 added/removed —— 订阅器首次刷新时快照从空到全量，
        diff 恒为"+全部"，会误清空已收敛的缓存(懒启动场景)。注册表状态跨订阅周期持久，
        以它为准可区分"真实实例集合变化"与"订阅器冷启动"。
        """
        reg_added, reg_removed = self.registry.sync_from_nacos(self.subscriber.get_instances())
        if reg_added or reg_removed:
            self.tools.clear()

    async def _ensure_started(self) -> None:
        if not self._started:
            await self.start()

    # ==================== 实例选择 ====================

    async def pick_instance(self) -> InstanceState:
        """供 SseMcpClient 等直连场景: 确保启动 -> 注册表 LB 选实例。"""
        await self._ensure_started()
        inst = self.registry.pick()
        if inst is None:
            self.subscriber.refresh_soon()
            raise McpNoInstanceError("无可用 MCP 实例(未发现健康实例或全部熔断)")
        return inst

    # ==================== 工具收敛 ====================

    async def _load_tools(self) -> bool:
        """从实例 listTools 并合并进收敛注册表(串行化)。"""
        async with self._load_lock:
            inst = self.registry.pick()
            if inst is None:
                logger.warning("工具收敛失败: 无可用实例")
                return False
            url = build_sse_url(inst.ip, inst.port)
            try:
                async with SseMcpClient(url) as client:
                    raw_tools = await client.list_tools()
                count = self.tools.update(raw_tools, provider=inst.key)
                self.registry.report_success(inst)
                logger.info("工具收敛完成: %d 个工具(来源实例 %s)", count, inst.key)
                return True
            except Exception as e:  # noqa: BLE001
                self.registry.report_failure(inst)
                self.subscriber.refresh_soon()
                logger.warning("listTools 失败(实例 %s): %s", inst.key, e)
                return False

    async def _ensure_tools(self) -> None:
        await self._ensure_started()
        if self.tools.empty or self.tools.stale:
            await self._load_tools()

    async def list_tools(self) -> list[dict]:
        """收敛后的工具清单(dict 形式，含 providers)。"""
        await self._ensure_tools()
        return [t.as_dict() for t in self.tools.all()]

    async def get_langchain_tools(self) -> list:
        """LangChain 工具列表(异常降级为空列表，保持旧行为不阻塞主流程)。"""
        try:
            await self._ensure_tools()
        except Exception as e:  # noqa: BLE001
            logger.error("获取 MCP 工具失败(降级空列表): %s", e)
            return []
        return [
            McpRemoteTool(name=spec.name, description=spec.description, args_schema=spec.args_model)
            for spec in self.tools.all()
        ]

    async def get_langchain_tool_by_name(self, tool_name: str):
        await self._ensure_tools()
        spec = self.tools.get(tool_name)
        if spec is None:
            logger.warning("MCP Server 未暴露工具 %s(可选工具，调用方将降级处理)", tool_name)
            return None
        return McpRemoteTool(name=spec.name, description=spec.description, args_schema=spec.args_model)

    # ==================== 调用(校验 + LB + 重试) ====================

    async def call_tool(self, tool_name: str, arguments: dict | None = None) -> str:
        """经网关调用 MCP 工具: 存在性校验 -> 参数本地校验 -> LB 选实例 -> 失败换实例重试。"""
        self._stats["call_total"] += 1
        # 1) 工具存在性校验(缓存空/失效时先收敛一次)
        spec = self.tools.get(tool_name)
        if spec is None:
            await self._ensure_tools()
            spec = self.tools.get(tool_name)
        if spec is None:
            self._stats["validate_blocked"] += 1
            raise McpToolNotFoundError(f"MCP 工具不存在: {tool_name}")
        # 2) 参数本地校验(fail-fast，不耗网络)
        try:
            args = spec.validate_arguments(arguments)
        except ValidationError as e:
            self._stats["validate_blocked"] += 1
            raise McpValidationError(f"工具 {tool_name} 参数校验失败: {e}") from e
        # 3) LB 选实例调用，失败换实例重试
        attempts = 1 + max(0, MCP_CALL_RETRIES)
        tried: set[str] = set()
        last_error: Exception | None = None
        for attempt in range(attempts):
            if attempt > 0:
                self._stats["retry_total"] += 1
            inst = self.registry.pick(exclude=tried)
            if inst is None:
                self.subscriber.refresh_soon()
                break
            tried.add(inst.key)
            url = build_sse_url(inst.ip, inst.port)
            try:
                async with self.registry.track(inst):
                    async with SseMcpClient(url) as client:
                        result = await client.call_tool(tool_name, args)
                self.registry.report_success(inst)
                self._stats["call_success"] += 1
                return tool_result_text(result)
            except Exception as e:  # noqa: BLE001
                last_error = e
                self.registry.report_failure(inst)
                self.subscriber.refresh_soon()
                logger.warning("callTool %s 失败(实例 %s, 第 %d/%d 次尝试): %s",
                               tool_name, inst.key, attempt + 1, attempts, e)
        self._stats["call_failure"] += 1
        if last_error is not None:
            raise McpCallError(f"调用 {tool_name} 失败(已尝试 {attempts} 个实例): {last_error}") from last_error
        raise McpNoInstanceError("无可用 MCP 实例(未发现健康实例或全部熔断)")

    # ==================== 观测 ====================

    def stats(self) -> dict:
        return {
            "started": self._started,
            "lb_policy": self.registry.policy,
            "nacos": self.subscriber.status(),
            "instances": self.registry.snapshot(),
            "tools": self.tools.status(),
            "calls": dict(self._stats),
        }


# 进程级单例: 订阅任务/注册表/工具缓存全局唯一
mcp_gateway = MCPGateway()