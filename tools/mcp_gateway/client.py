"""MCP 客户端连接层：SSE/Streamable HTTP 传输、结果解析、SpringAI 兼容(自 tools/mcp.py 平移增强)。"""
import asyncio
import json
import logging
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any

import mcp.types as mcp_types
from langchain_core.tools import BaseTool
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from pydantic import BaseModel

from tools.mcp_gateway.config import MCP_SSE_PATH, MCP_TIMEOUT

logger = logging.getLogger("yibot.mcp.client")


def _patch_call_tool_result_for_springai() -> None:
    """
    兼容 SpringAI MCP Server: 它会把 List 结果直接放进 structuredContent，
    而 MCP 规范与 Python SDK 均要求 object，严格校验会导致 callTool 响应解析失败
    (服务端实际执行成功，数据已返回)。这里放宽该字段类型为 Any，不影响 content 文本。
    """
    try:
        field = mcp_types.CallToolResult.model_fields.get("structuredContent")
        if field is not None and field.annotation is not Any:
            field.annotation = Any
            mcp_types.CallToolResult.model_rebuild(force=True)
            logger.info("已启用 SpringAI structuredContent 兼容(list 结果不再解析失败)")
    except Exception as e:  # noqa: BLE001
        logger.warning("SpringAI structuredContent 兼容 patch 失败: %s", e)


_patch_call_tool_result_for_springai()


def build_sse_url(ip: str, port: int | str, sse_path: str | None = None) -> str:
    """拼出 MCP Server 的 SSE 端点地址，如 http://127.0.0.1:8090/mcp。"""
    path = (sse_path or MCP_SSE_PATH).strip()
    if not path.startswith("/"):
        path = "/" + path
    return f"http://{ip}:{port}{path}"


def tool_result_text(result: Any) -> str:
    """
    把 MCP callTool 的返回结果拼成纯文本:
    - content 文本块(SDK 标准输出)
    - structuredContent 结构化数据(SpringAI 常把真实数据放在这里，可能是 list / dict)
    """
    if result is None:
        return ""
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)

    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        try:
            parts.append(json.dumps(structured, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            parts.append(str(structured))

    return "\n".join(parts) or str(result)


async def _connect_session(url: str, headers: dict[str, str] | None = None) -> tuple[AsyncExitStack, ClientSession]:
    """
    建立到 MCP Server 的连接并完成 initialize 握手:
    优先 SSE 传输(GET {url} 建立事件流)，失败时自动降级 Streamable HTTP(POST {url})。
    返回 (stack, session)，调用方负责 stack.aclose() 释放连接。
    """
    stack = AsyncExitStack()
    streams = None
    sse_error: Exception | None = None
    try:
        streams = await stack.enter_async_context(
            sse_client(url, headers=headers, timeout=MCP_TIMEOUT, sse_read_timeout=300.0)
        )
    except Exception as e:  # noqa: BLE001
        sse_error = e

    if streams is None:
        try:
            streams = await stack.enter_async_context(
                streamablehttp_client(url, headers=headers, timeout=MCP_TIMEOUT, sse_read_timeout=300.0)
            )
        except Exception as http_error:  # noqa: BLE001
            await stack.aclose()
            raise RuntimeError(
                f"SSE 与 Streamable HTTP 均无法连接 {url} | SSE 错误: {sse_error} | StreamableHTTP 错误: {http_error}"
            ) from http_error

    # SSE 传输返回 (read, write) 二元组，Streamable HTTP 返回 (read, write, get_session_id) 三元组
    read_stream, write_stream = streams[0], streams[1]
    session = await stack.enter_async_context(
        ClientSession(read_stream, write_stream, read_timeout_seconds=timedelta(seconds=MCP_TIMEOUT))
    )
    await session.initialize()
    return stack, session


# ==================== MCP 客户端封装 ====================

class SseMcpClient:
    """基于 SSE/Streamable HTTP 传输的 MCP 客户端。

    用法:
        async with SseMcpClient(url) as client:   # 显式指定实例地址(脚本/测试)
        async with SseMcpClient() as client:      # 不传 url 时经网关 LB 选择实例
            tools = await client.list_tools()
            result = await client.call_tool("queryWeather", {"city": "北京"})
    """

    def __init__(self, sse_url: str | None = None, headers: dict[str, str] | None = None):
        self.sse_url = sse_url
        self.headers = headers
        self.session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None

    async def connect(self) -> "SseMcpClient":
        """建立 MCP 连接并完成 initialize 握手；不传 url 时经网关负载均衡选择实例。"""
        if self.session is not None:
            return self

        if self.sse_url:
            url = self.sse_url
        else:
            from tools.mcp_gateway.gateway import mcp_gateway  # 函数级导入避免循环依赖
            inst = await mcp_gateway.pick_instance()
            url = build_sse_url(inst.ip, inst.port)

        try:
            self._stack, self.session = await _connect_session(url, headers=self.headers)
            logger.info("已建立 MCP 连接: %s", url)
        except BaseException:
            await self.close()
            raise
        return self

    async def close(self) -> None:
        """关闭 MCP 会话与传输连接。"""
        self.session = None
        if self._stack is not None:
            stack, self._stack = self._stack, None
            try:
                await stack.aclose()
            except Exception as e:  # noqa: BLE001
                logger.warning("关闭 MCP 连接时出错: %s", e)

    async def list_tools(self) -> list:
        """listTools: 返回服务端工具列表(name / description / inputSchema)。"""
        if self.session is None:
            raise RuntimeError("MCP 会话未建立，请先 connect() 或使用 async with")
        result = await self.session.list_tools()
        return result.tools or []

    async def call_tool(self, tool_name: str, arguments: dict | None = None):
        """callTool: 调用服务端工具并返回原始 CallToolResult。"""
        if self.session is None:
            raise RuntimeError("MCP 会话未建立，请先 connect() 或使用 async with")
        return await self.session.call_tool(tool_name, arguments or {})

    async def __aenter__(self) -> "SseMcpClient":
        return await self.connect()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()


# ==================== LangChain 工具包装 ====================

class McpRemoteTool(BaseTool):
    """
    把远端 MCP 工具包装成 LangChain 工具(网关模式):
    每次调用经网关完成 本地参数校验 -> LB 选实例 -> 建连调用 -> 失败换实例重试 -> 熔断上报，
    不再各自直连 Nacos/实例。
    """

    name: str
    description: str = ""
    args_schema: type[BaseModel] | None = None

    def _run(self, **kwargs: Any) -> Any:
        # 同步路径(调用方无事件循环时)；注意不可在已运行的事件循环内调用
        return asyncio.run(self._call_remote(kwargs))

    async def _arun(self, **kwargs: Any) -> Any:
        # 异步路径(agent.ainvoke / graph.ainvoke)
        return await self._call_remote(kwargs)

    async def _call_remote(self, arguments: dict) -> str:
        from tools.mcp_gateway.gateway import mcp_gateway  # 函数级导入避免循环依赖
        return await mcp_gateway.call_tool(self.name, arguments)
