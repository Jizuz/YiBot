#!/usr/bin/env python3
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from tools.mcp import StreamableMcpClient

def test_mcp_connection():
    try:
        client = StreamableMcpClient("http://127.0.0.1:8090")
        print("正在初始化 MCP 连接...")
        result = client.initialize()
        print("✅ MCP 初始化成功:", result)
        
        print("正在获取工具列表...")
        tools_result = client.list_tools()
        print("✅ 工具列表获取成功:", tools_result)
        
        if tools_result.get("result", {}).get("tools"):
            print(f"✅ 成功加载 {len(tools_result['result']['tools'])} 个工具")
            for tool in tools_result['result']['tools']:
                print(f"  - {tool['name']}: {tool['description']}")
        else:
            print("⚠️ 未找到任何工具")
            
    except Exception as e:
        print(f"❌ 测试失败: {str(e)}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_mcp_connection()