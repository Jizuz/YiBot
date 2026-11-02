"""MCP 工具层兼容门面(实现已迁移至 tools/mcp_gateway 包，见技改 §4.4)。

本文件保留原有公开接口，供既有调用方(education/rights/chit_chat Agent、test_mcp.py)零改动使用:
- get_mcp_tools / get_mcp_tool_by_name: 走网关(Nacos 订阅 + 熔断 + LB + 工具收敛)
- SseMcpClient / McpRemoteTool / build_sse_url / tool_result_text: 自 client 平移
- MCP_TIMEOUT / TOOL_* / SEARCH_KB_TOOL_DESC: 自 config 平移
- discover_mcp_server / list_mcp_tools / call_mcp_tool: 一次性便捷接口(脚本/调试用)
"""
import asyncio
import random
from typing import Any

import httpx
from langchain_core.tools import BaseTool

from tools.mcp_gateway.config import (
    MCP_TIMEOUT,
    SEARCH_KB_TOOL_DESC,
    TOOL_GET_NEARBY_STORES,
    TOOL_GET_RIGHT_LIST,
    TOOL_GET_WEATHER,
    TOOL_SEARCH_KNOWLEDGE_BASE,
)
from tools.mcp_gateway.client import SseMcpClient, build_sse_url
from tools.mcp_gateway.gateway import McpRemoteTool, tool_result_text
from tools.mcp_gateway.gateway import mcp_gateway
from tools.mcp_gateway.config import (
    MCP_SERVICE_NAME,
    NACOS_GROUP,
    NACOS_NAMESPACE,
    NACOS_PASSWORD,
    NACOS_SERVER_ADDR,
    NACOS_USERNAME,
)
from tools.mcp_gateway.nacos_subscriber import extract_healthy_instances, nacos_base_url

__all__ = [
    "MCP_TIMEOUT", "TOOL_GET_WEATHER", "TOOL_GET_RIGHT_LIST", "TOOL_GET_NEARBY_STORES",
    "TOOL_SEARCH_KNOWLEDGE_BASE", "SEARCH_KB_TOOL_DESC",
    "SseMcpClient", "McpRemoteTool", "build_sse_url", "tool_result_text",
    "discover_mcp_server", "list_mcp_tools", "call_mcp_tool",
    "get_mcp_tools", "get_mcp_tool_by_name", "mcp_gateway",
]


# ==================== 一次性同步发现(脚本/调试用；生产路径走 mcp_gateway 订阅) ====================

def _nacos_access_token_sync() -> str | None:
    """Nacos 开启鉴权时登录换取 accessToken；未配置账号或失败时返回 None(匿名访问)。"""
    if not NACOS_USERNAME:
        return None
    try:
        resp = httpx.post(
            f"{nacos_base_url()}/nacos/v1/auth/login",
            data={"username": NACOS_USERNAME, "password": NACOS_PASSWORD},
            timeout=MCP_TIMEOUT,
        )
        resp.raise_for_status()
        return (resp.json() or {}).get("accessToken") or None
    except Exception:  # noqa: BLE001
        return None


def discover_mcp_server(service_name: str | None = None) -> dict:
    """从 Nacos 查询健康实例(healthyOnly=true)并随机挑选一个(一次性，不经网关缓存)。"""
    params: dict[str, Any] = {
        "serviceName": service_name or MCP_SERVICE_NAME,
        "groupName": NACOS_GROUP,
        "healthyOnly": "true",
    }
    if NACOS_NAMESPACE:
        params["namespaceId"] = NACOS_NAMESPACE
    token = _nacos_access_token_sync()
    if token:
        params["accessToken"] = token

    base = nacos_base_url()
    errors: list[str] = []
    for api_path in ("/nacos/v1/ns/instance/list", "/nacos/v2/ns/instance/list"):
        try:
            resp = httpx.get(f"{base}{api_path}", params=params, timeout=MCP_TIMEOUT)
            if resp.status_code != 200:
                errors.append(f"{api_path} -> HTTP {resp.status_code}: {resp.text[:120]}")
                continue
            instances = extract_healthy_instances(resp.json())
            if not instances:
                errors.append(f"{api_path} -> 无健康实例: {resp.text[:120]}")
                continue
            return random.choice(instances)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{api_path} -> {e}")

    raise RuntimeError(
        f"Nacos 未发现健康实例(服务: {params['serviceName']}, 分组: {NACOS_GROUP}): " + "; ".join(errors)
    )


# ==================== 便捷同步接口(走网关: 校验/LB/熔断/收敛) ====================
# 注意: 不可在已运行的事件循环内调用(如 FastAPI 异步路由)，异步场景请直接 await mcp_gateway.*

async def _list_tools_once() -> list[dict]:
    return await mcp_gateway.list_tools()


async def _call_tool_once(tool_name: str, arguments: dict | None = None) -> str:
    return await mcp_gateway.call_tool(tool_name, arguments)


def list_mcp_tools() -> list[dict]:
    """同步 listTools: 返回收敛后的工具清单(name/description/inputSchema)。"""
    return asyncio.run(_list_tools_once())


def call_mcp_tool(tool_name: str, arguments: dict | None = None) -> str:
    """同步 callTool: 经网关(校验 -> LB -> 重试)并返回文本结果。"""
    return asyncio.run(_call_tool_once(tool_name, arguments))


# ==================== Agent 主入口(零改动兼容) ====================

async def get_mcp_tools() -> list[BaseTool]:
    """供 LangChain Agent 使用的主入口: 经网关收敛工具列表；失败降级空列表。"""
    return await mcp_gateway.get_langchain_tools()


async def get_mcp_tool_by_name(tool_name: str) -> BaseTool | None:
    """按名称获取单个 MCP 工具(可选工具模式)；未暴露/网关异常返回 None 由调用方降级。"""
    try:
        return await mcp_gateway.get_langchain_tool_by_name(tool_name)
    except Exception:  # noqa: BLE001
        return None
