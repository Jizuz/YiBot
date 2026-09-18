"""
MCP 客户端模块(基于 Nacos 服务发现 + SSE / Streamable HTTP 传输)

完整链路(per-call 模式: 每次调用独立建连、用完即关,天然免疫连接失效):
    1. 从 Nacos 拿到 SpringAI MCP Server 健康实例(healthyOnly=true)
    2. 与实例建立 MCP 连接(SSE 优先,失败自动降级 Streamable HTTP): http://{ip}:{port}{MCP_SSE_PATH}
    3. initialize -> listTools / callTool

SpringAI MCP Server 暴露的工具(以 listTools 实际返回为准):
    - queryWeather         查天气
    - get_right_list       查用户权益
    - get_nearby_stores    查附近门店

对外入口:
    - get_mcp_tools()       供 LangChain Agent 使用: Nacos -> 连接 -> LangChain 工具列表
    - list_mcp_tools()      同步 listTools
    - call_mcp_tool()       同步 callTool
    - SseMcpClient          异步上下文管理器,一次性连接用法(自动做 Nacos 发现)
    - discover_mcp_server() 仅做 Nacos 服务发现,返回健康实例
"""

import asyncio
import json
import os
import random
import traceback
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any

import httpx
from dotenv import load_dotenv
from langchain_core.tools import BaseTool
from mcp import ClientSession
import mcp.types as mcp_types
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from pydantic import BaseModel, Field, create_model

load_dotenv()

# ==================== 配置(优先读环境变量 / .env) ====================

# Nacos 地址,如 127.0.0.1:8848 或 http://127.0.0.1:8848
NACOS_SERVER_ADDR = os.getenv("NACOS_SERVER_ADDR", "127.0.0.1:8848")
# Nacos 鉴权账号(未开启鉴权时留空即可)
NACOS_USERNAME = os.getenv("NACOS_USERNAME", "").strip()
NACOS_PASSWORD = os.getenv("NACOS_PASSWORD", "").strip()
# 命名空间 ID(public 命名空间留空)
NACOS_NAMESPACE = os.getenv("NACOS_NAMESPACE", "").strip()
NACOS_GROUP = os.getenv("NACOS_GROUP", "DEFAULT_GROUP").strip()

# SpringAI MCP Server 注册到 Nacos 的服务名
MCP_SERVICE_NAME = os.getenv("MCP_SERVICE_NAME", "spring-ai-mcp-server")
# SpringAI MCP Server(WebMVC-SSE)默认的 SSE 端点路径
MCP_SSE_PATH = os.getenv("MCP_SSE_PATH", "/sse")
# 连接 / 调用超时(秒)
MCP_TIMEOUT = float(os.getenv("MCP_TIMEOUT", "30"))

# MCP Server 暴露的工具名(已按当前 SpringAI MCP Server listTools 实际返回配置)
TOOL_GET_WEATHER = "queryWeather"            # 查天气,参数: city
TOOL_GET_RIGHT_LIST = "get_right_list"       # 查用户权益,参数: userId(int)
TOOL_GET_NEARBY_STORES = "get_nearby_stores"  # 查附近门店,参数: location


def _patch_call_tool_result_for_springai() -> None:
    """
    兼容 SpringAI MCP Server: 它会把 List 结果直接放进 structuredContent,
    而 MCP 规范与 Python SDK 均要求 object,严格校验会导致 callTool 响应解析失败
    (服务端实际执行成功,数据已返回)。这里放宽该字段类型为 Any,不影响 content 文本。
    """
    try:
        field = mcp_types.CallToolResult.model_fields.get("structuredContent")
        if field is not None and field.annotation is not Any:
            field.annotation = Any
            mcp_types.CallToolResult.model_rebuild(force=True)
            print("✅ 已启用 SpringAI structuredContent 兼容(list 结果不再解析失败)")
    except Exception as e:
        print(f"⚠️ SpringAI structuredContent 兼容 patch 失败: {e}")


_patch_call_tool_result_for_springai()


# ==================== 一、Nacos 服务发现 ====================

def _nacos_base_url() -> str:
    """规范化 Nacos 服务端地址,统一带协议前缀且不带末尾斜杠"""
    addr = NACOS_SERVER_ADDR.strip().rstrip("/")
    if not addr.startswith(("http://", "https://")):
        addr = f"http://{addr}"
    return addr


def _nacos_access_token() -> str | None:
    """Nacos 开启鉴权时登录换取 accessToken;未配置账号或失败时返回 None(匿名访问)"""
    if not NACOS_USERNAME:
        return None
    try:
        resp = httpx.post(
            f"{_nacos_base_url()}/nacos/v1/auth/login",
            data={"username": NACOS_USERNAME, "password": NACOS_PASSWORD},
            timeout=MCP_TIMEOUT,
        )
        resp.raise_for_status()
        token = (resp.json() or {}).get("accessToken")
        return token or None
    except Exception as e:
        print(f"⚠️ Nacos 登录失败,将尝试匿名访问: {e}")
        return None


def _extract_healthy_instances(payload: Any) -> list[dict]:
    """从 Nacos 响应中提取健康实例,兼容 v1(hosts)与 v2(data / data.list)两种结构"""
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


def discover_mcp_server(service_name: str | None = None) -> dict:
    """
    从 Nacos 查询 SpringAI MCP Server 的健康实例(healthyOnly=true),
    随机挑选一个实现简单负载均衡。

    :param service_name: 服务名,默认取 MCP_SERVICE_NAME
    :return: 健康实例 dict,含 ip / port / instanceId / metadata 等字段
    """
    params: dict[str, Any] = {
        "serviceName": service_name or MCP_SERVICE_NAME,
        "groupName": NACOS_GROUP,
        "healthyOnly": "true",
    }
    if NACOS_NAMESPACE:
        params["namespaceId"] = NACOS_NAMESPACE

    token = _nacos_access_token()
    if token:
        params["accessToken"] = token

    base = _nacos_base_url()
    errors: list[str] = []
    # 兼容 Nacos 1.x/2.x 的 v1 接口与 2.x/3.x 的 v2 接口,依次尝试
    for api_path in ("/nacos/v1/ns/instance/list", "/nacos/v2/ns/instance/list"):
        try:
            resp = httpx.get(f"{base}{api_path}", params=params, timeout=MCP_TIMEOUT)
            if resp.status_code != 200:
                errors.append(f"{api_path} -> HTTP {resp.status_code}: {resp.text[:120]}")
                continue
            instances = _extract_healthy_instances(resp.json())
            if not instances:
                errors.append(f"{api_path} -> 无健康实例: {resp.text[:120]}")
                continue
            instance = random.choice(instances)
            print(f"✅ Nacos 发现 MCP Server 健康实例: {instance['ip']}:{instance['port']}(共 {len(instances)} 个)")
            return instance
        except Exception as e:
            errors.append(f"{api_path} -> {e}")

    raise RuntimeError(
        f"❌ Nacos 未发现健康实例(服务: {params['serviceName']}, 分组: {NACOS_GROUP}): " + "; ".join(errors)
    )


def build_sse_url(ip: str, port: int | str, sse_path: str | None = None) -> str:
    """拼出 MCP Server 的 SSE 端点地址,如 http://127.0.0.1:8090/sse"""
    path = (sse_path or MCP_SSE_PATH).strip()
    if not path.startswith("/"):
        path = "/" + path
    return f"http://{ip}:{port}{path}"


# ==================== 二、结果解析 ====================

def tool_result_text(result: Any) -> str:
    """
    把 MCP callTool 的返回结果拼成纯文本:
    - content 文本块(SDK 标准输出)
    - structuredContent 结构化数据(SpringAI 常把真实数据放在这里,可能是 list / dict)
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
        except Exception:
            parts.append(str(structured))

    return "\n".join(parts) or str(result)


# ==================== 三、SSE MCP 客户端(一次性连接用法) ====================

async def _connect_session(url: str, headers: dict[str, str] | None = None) -> tuple[AsyncExitStack, ClientSession]:
    """
    建立到 MCP Server 的连接并完成 initialize 握手:
    优先 SSE 传输(GET {url} 建立事件流,SpringAI WebMVC-SSE 默认),
    失败时自动降级 Streamable HTTP(POST {url}, 响应同为 text/event-stream 流)。
    返回 (stack, session),调用方负责 stack.aclose() 释放连接。
    """
    stack = AsyncExitStack()
    streams = None
    sse_error: Exception | None = None
    try:
        streams = await stack.enter_async_context(
            sse_client(url, headers=headers, timeout=MCP_TIMEOUT, sse_read_timeout=300.0)
        )
    except Exception as e:
        sse_error = e  # 传统 SSE 端点不存在 / 握手失败,尝试 Streamable HTTP

    if streams is None:
        try:
            streams = await stack.enter_async_context(
                streamablehttp_client(url, headers=headers, timeout=MCP_TIMEOUT, sse_read_timeout=300.0)
            )
        except Exception as http_error:
            await stack.aclose()
            raise RuntimeError(
                f"SSE 与 Streamable HTTP 均无法连接 {url} | SSE 错误: {sse_error} | StreamableHTTP 错误: {http_error}"
            ) from http_error

    # SSE 传输返回 (read, write) 二元组,Streamable HTTP 返回 (read, write, get_session_id) 三元组
    read_stream, write_stream = streams[0], streams[1]
    session = await stack.enter_async_context(
        ClientSession(read_stream, write_stream, read_timeout_seconds=timedelta(seconds=MCP_TIMEOUT))
    )
    await session.initialize()
    return stack, session


class SseMcpClient:
    """
    基于 SSE 传输的 MCP 客户端,完整链路: Nacos 发现健康实例 -> 建立 SSE 连接 -> initialize。

    用法:
        async with SseMcpClient() as client:            # 不传 url 时自动走 Nacos 服务发现
            tools = await client.list_tools()           # listTools
            result = await client.call_tool("get_weather", {"city": "北京"})  # callTool
    """

    def __init__(self, sse_url: str | None = None, headers: dict[str, str] | None = None):
        """
        :param sse_url: SSE 端点地址;不传则从 Nacos 发现健康实例后自动拼接
        :param headers: 建立 SSE 连接时携带的自定义请求头
        """
        self.sse_url = sse_url
        self.headers = headers
        self.session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None

    async def connect(self) -> "SseMcpClient":
        """建立 SSE 连接并完成 MCP initialize 握手"""
        if self.session is not None:
            return self

        if self.sse_url:
            url = self.sse_url
        else:
            instance = await asyncio.to_thread(discover_mcp_server)
            url = build_sse_url(instance["ip"], instance["port"])

        try:
            self._stack, self.session = await _connect_session(url, headers=self.headers)
            print(f"✅ 已建立 MCP 连接: {url}")
        except BaseException:
            await self.close()
            raise
        return self

    async def close(self) -> None:
        """关闭 MCP 会话与 SSE 连接"""
        self.session = None
        if self._stack is not None:
            stack, self._stack = self._stack, None
            try:
                await stack.aclose()
            except Exception as e:
                print(f"⚠️ 关闭 MCP 连接时出错: {e}")

    async def list_tools(self) -> list:
        """listTools: 返回服务端工具列表(name / description / inputSchema)"""
        if self.session is None:
            raise RuntimeError("MCP 会话未建立,请先 connect() 或使用 async with")
        result = await self.session.list_tools()
        return result.tools or []

    async def call_tool(self, tool_name: str, arguments: dict | None = None):
        """callTool: 调用服务端工具并返回原始 CallToolResult"""
        if self.session is None:
            raise RuntimeError("MCP 会话未建立,请先 connect() 或使用 async with")
        return await self.session.call_tool(tool_name, arguments or {})

    async def __aenter__(self) -> "SseMcpClient":
        return await self.connect()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()


# ==================== 四、LangChain 工具包装 ====================

_JSON_SCHEMA_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "bool": bool,
    "array": list,
    "object": dict,
}


def _schema_to_args_model(tool_name: str, input_schema: dict | None) -> type[BaseModel]:
    """由 MCP 工具的 inputSchema(JSON Schema)动态生成 pydantic 参数模型"""
    schema = input_schema or {}
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])

    fields: dict[str, tuple] = {}
    for field_name, field_def in properties.items():
        field_def = field_def or {}
        py_type = _JSON_SCHEMA_TYPE_MAP.get(str(field_def.get("type", "string")).lower(), str)
        description = str(field_def.get("description") or "")
        if field_name in required:
            fields[field_name] = (py_type, Field(..., description=description))
        else:
            fields[field_name] = (py_type, Field(default=field_def.get("default"), description=description))

    safe_name = "".join(c if c.isalnum() else "_" for c in tool_name)
    return create_model(f"McpArgs_{safe_name}", **fields) if fields else None


class McpRemoteTool(BaseTool):
    """
    把远端 MCP 工具包装成 LangChain 工具(per-call 模式):
    每次调用独立完成 Nacos 发现 -> 连接 -> callTool -> 关闭,天然免疫连接失效,
    无需维持后台长连接或跨线程桥接。
    """

    name: str
    description: str = ""
    args_schema: type[BaseModel] | None = None

    def _run(self, **kwargs: Any) -> Any:
        # 同步路径(调用方无事件循环时): 每次调用独立建连
        return asyncio.run(self._call_remote(kwargs))

    async def _arun(self, **kwargs: Any) -> Any:
        # 异步路径(agent.ainvoke / graph.ainvoke): 每次调用独立建连
        return await self._call_remote(kwargs)

    async def _call_remote(self, arguments: dict) -> str:
        async with SseMcpClient() as client:
            result = await client.call_tool(self.name, arguments)
            return tool_result_text(result)


async def get_mcp_tools() -> list[BaseTool]:
    """
    供 LangChain Agent 使用的主入口:
    Nacos 发现健康实例 -> 建立连接 -> listTools -> 包装成 LangChain 工具列表。
    per-call 模式: 此处连接用完即关,工具实际调用时各自独立建连。
    失败时返回空列表,保证主流程可用。
    """
    try:
        async with SseMcpClient() as client:
            tools = await client.list_tools()
    except Exception as e:
        print(f"❌ 获取 MCP 工具失败: {e}")
        traceback.print_exc()
        return []

    lang_tools: list[BaseTool] = []
    for tool in tools:
        lang_tools.append(
            McpRemoteTool(
                name=tool.name,
                description=(tool.description or "").strip() or f"MCP 工具 {tool.name}",
                args_schema=_schema_to_args_model(tool.name, tool.inputSchema),
            )
        )
    print(f"✅ MCP 连接成功,共加载 {len(lang_tools)} 个工具: {[t.name for t in tools]}")
    return lang_tools


# ==================== 五、同步便捷接口 ====================

async def _list_tools_once() -> list:
    """一次性连接完成 listTools"""
    async with SseMcpClient() as client:
        return await client.list_tools()


async def _call_tool_once(tool_name: str, arguments: dict | None = None) -> str:
    """一次性连接完成 callTool 并提取文本"""
    async with SseMcpClient() as client:
        result = await client.call_tool(tool_name, arguments)
        return tool_result_text(result)


def list_mcp_tools() -> list[dict]:
    """同步 listTools: 每次独立建连(注意: 不可在已运行的事件循环内调用)"""
    return [
        {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
        for t in asyncio.run(_list_tools_once())
    ]


def call_mcp_tool(tool_name: str, arguments: dict | None = None) -> str:
    """
    同步 callTool: 每次独立建连(注意: 不可在已运行的事件循环内调用)。
    如: call_mcp_tool("queryWeather", {"city": "北京"})
    """
    return asyncio.run(_call_tool_once(tool_name, arguments))


# ==================== 六、完整链路演示 ====================

async def _demo() -> None:
    # 1. Nacos 服务发现:拿到 SpringAI MCP Server 健康实例
    print("========== 1. Nacos 服务发现 ==========")
    instance = discover_mcp_server()
    print(f"选中健康实例: {instance['ip']}:{instance['port']}")

    # 2. 建立 SSE MCP 连接 + 3. listTools
    print("\n========== 2. 建立 SSE MCP 连接 & listTools ==========")
    async with SseMcpClient(build_sse_url(instance["ip"], instance["port"])) as client:
        tools = await client.list_tools()
        print(f"共发现 {len(tools)} 个工具:")
        available = set()
        for tool in tools:
            available.add(tool.name)
            print(f"  - {tool.name}: {tool.description}")
            print(f"    参数: {tool.inputSchema}")

        # 4. callTool: 查天气 / 查用户权益 / 查附近门店
        print("\n========== 3. callTool ==========")
        demo_calls = [
            (TOOL_GET_WEATHER, {"city": "北京"}),
            (TOOL_GET_RIGHT_LIST, {"userId": 10001}),
            (TOOL_GET_NEARBY_STORES, {"location": "北京市朝阳区望京"}),
        ]
        for tool_name, arguments in demo_calls:
            if tool_name not in available:
                print(f"⚠️ 服务端未暴露工具 {tool_name},跳过(请以 listTools 结果为准)")
                continue
            try:
                result = await client.call_tool(tool_name, arguments)
                print(f"\n>>> 调用 {tool_name} {arguments}\n{tool_result_text(result)}")
            except Exception as e:
                print(f"❌ 调用 {tool_name} 失败: {e}")


if __name__ == "__main__":
    asyncio.run(_demo())



