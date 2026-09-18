#!/usr/bin/env python3
"""MCP 完整链路测试: Nacos 发现健康实例 -> 建立 SSE MCP 连接 -> listTools / callTool"""
import asyncio
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from tools.mcp import (
    TOOL_GET_NEARBY_STORES,
    TOOL_GET_RIGHT_LIST,
    TOOL_GET_WEATHER,
    SseMcpClient,
    build_sse_url,
    call_mcp_tool,
    discover_mcp_server,
    list_mcp_tools,
    tool_result_text,
)


def test_nacos_discovery():
    """1. 从 Nacos 拿到 SpringAI MCP Server 健康实例"""
    instance = discover_mcp_server()
    assert instance and instance.get("ip") and instance.get("port"), f"健康实例信息不完整: {instance}"
    print("✅ Nacos 健康实例发现成功:", instance["ip"], instance["port"])
    return instance


async def test_sse_list_and_call():
    """2. 建立 SSE MCP 连接 -> listTools -> callTool"""
    instance = test_nacos_discovery()
    sse_url = build_sse_url(instance["ip"], instance["port"])
    print(f"SSE 端点: {sse_url}")

    async with SseMcpClient(sse_url) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        print(f"✅ listTools 成功,共 {len(tools)} 个工具: {sorted(names)}")
        assert tools, "未发现任何 MCP 工具"

        # callTool 演示: 只调用服务端实际暴露的工具
        demo_calls = [
            (TOOL_GET_WEATHER, {"city": "北京"}),
            (TOOL_GET_RIGHT_LIST, {"userId": 10001}),
            (TOOL_GET_NEARBY_STORES, {"location": "北京市朝阳区望京"}),
        ]
        for tool_name, arguments in demo_calls:
            if tool_name not in names:
                print(f"⚠️ 服务端未暴露 {tool_name},跳过")
                continue
            result = await client.call_tool(tool_name, arguments)
            print(f"✅ callTool {tool_name} 成功: {tool_result_text(result)}")


def test_sync_helpers():
    """3. 同步便捷接口(per-call 独立建连,供非异步场景直接调用)"""
    tools = list_mcp_tools()
    print(f"✅ list_mcp_tools 成功: {[t['name'] for t in tools]}")
    names = {t["name"] for t in tools}
    if TOOL_GET_WEATHER in names:
        print("✅ call_mcp_tool:", call_mcp_tool(TOOL_GET_WEATHER, {"city": "北京"}))


if __name__ == "__main__":
    try:
        asyncio.run(test_sse_list_and_call())
        test_sync_helpers()
        print("\n🎉 全部 MCP 测试通过")
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
