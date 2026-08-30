# import asyncio
# import threading
# from typing import Any
# from anyio import from_thread
# from functools import wraps
# import inspect
# import httpx
# from langchain_mcp_adapters.client import MultiServerMCPClient
# from langchain_mcp_adapters.tools import load_mcp_tools
# from langchain.tools import BaseTool

# import sys

# from pydantic import BaseModel
# import requests
# if "uvloop" in sys.modules:
#     del sys.modules["uvloop"]
# # 强制使用原生 asyncio 事件循环
# if sys.platform == "win32":
#     asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
# else:
#     asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())

# def sync_wrapper(async_func):
#     """
#     兼容原生asyncio：新开线程运行协程，不依赖anyio，解决 Not running inside an AnyIO worker thread
#     """
#     @wraps(async_func)
#     def sync_call(*args, **kwargs):
#         result_container = {}
#         exc_container = {}

#         def thread_body():
#             try:
#                 # 在子线程新建独立event_loop执行异步函数
#                 loop = asyncio.new_event_loop()
#                 asyncio.set_event_loop(loop)
#                 res = loop.run_until_complete(async_func(*args, **kwargs))
#                 result_container["res"] = res
#             except Exception as e:
#                 exc_container["err"] = e
#             finally:
#                 # 确保事件循环被正确关闭
#                 if 'loop' in locals():
#                     loop.close()

#         t = threading.Thread(target=thread_body, daemon=True)
#         t.start()
#         t.join()

#         if "err" in exc_container:
#             raise exc_container["err"]
#         return result_container["res"]

#     return sync_call

# class StreamableMcpClient:
#     def __init__(self, base_url: str):
#         self.base_url = base_url.rstrip("/")
#         self.session_id: str | None = None
#         # 为每个请求创建新的客户端，避免事件循环问题
#         self.client = None

#     async def _rpc(self, payload: dict) -> dict:
#         headers = {
#             "Content-Type": "application/json",
#             "Accept": "text/event-stream, application/json"
#         }
#         if self.session_id:
#             headers["mcp-session-id"] = self.session_id

#         print(f"发送请求到 {self.base_url}/mcp")
#         print(f"请求头: {headers}")
#         print(f"请求体: {payload}")

#         # 为每个请求创建新的客户端，增加超时时间
#         client = httpx.AsyncClient(timeout=120.0)  # 增加超时时间到120秒  # 增加超时时间到60秒
#         try:
#             resp = await client.post(f"{self.base_url}/mcp", json=payload, headers=headers)
            
#             print(f"响应状态码: {resp.status_code}")
#             print(f"响应头: {dict(resp.headers)}")
#             print(f"响应内容: {resp.text[:200]}...")  # 只打印前200个字符
            
#             if resp.status_code != 200:
#                 print(f"服务器返回错误状态码: {resp.status_code}")
#                 print(f"完整响应内容: {resp.text}")
#                 resp.raise_for_status()
            
#             # 服务端会在响应头返回mcp‑session‑id
#             new_sid = resp.headers.get("mcp-session-id")
#             if new_sid:
#                 self.session_id = new_sid
#                 print(f"获取到新的 session ID: {new_sid}")
            
#             # 尝试解析 JSON 响应
#             try:
#                 result = resp.json()
#                 print(f"JSON 解析成功: {result}")
#                 return result
#             except Exception as e:
#                 print(f"JSON 解析失败: {str(e)}")
#                 print(f"响应内容: {resp.text}")
#                 # 尝试直接返回文本内容作为结果
#                 return {"jsonrpc": "2.0", "id": payload["id"], "result": resp.text}
#         finally:
#             await client.aclose()

#     @sync_wrapper
#     async def initialize(self):
#         """会话初始化，必须最先调用"""
#         payload = {
#             "jsonrpc": "2.0",
#             "id": 1,
#             "method": "initialize",
#             "params": {
#                 "protocolVersion": "2025-03-17",
#                 "capabilities": {},
#                 "clientInfo": {"name": "python-mcp-client", "version": "1.0.0"}
#             }
#         }
#         return await self._rpc(payload)

#     @sync_wrapper
#     async def list_tools(self):
#         payload = {"jsonrpc": "2.0", "id": 2, "method": "listTools", "params": {}}
#         return await self._rpc(payload)

#     @sync_wrapper
#     async def call_tool(self, name: str, arguments: dict):
#         payload = {
#             "jsonrpc": "2.0",
#             "id": 3,
#             "method": "callTool",
#             "params": {"name": name, "arguments": arguments}
#         }
#         return await self._rpc(payload)

#     @sync_wrapper
#     async def close(self):
#         await self.client.aclose()

# class McpLangTool(BaseTool):
#     """封装MCP工具，完整实现 _run（同步） / _arun（异步）"""
#     name: str
#     description: str
#     args_schema: type[BaseModel]
#     _mcp_client: StreamableMcpClient

#     def _run(self, **kwargs: Any) -> Any:
#         """同步调用入口，经过sync_wrapper封装"""
#         return self._mcp_client.call_tool(self.name, kwargs)

#     async def _arun(self, **kwargs: Any) -> Any:
#         """异步调用入口，直接调用底层原始async方法，避免装饰器套壳"""
#         raw_coroutine = self._mcp_client.call_tool.__wrapped__
#         return await raw_coroutine(self.name, kwargs)

# def build_mcp_lang_tools(mcp_base_url: str) -> tuple[StreamableMcpClient, list[BaseTool]]:
#     """
#     根据MCP服务地址，构建LangChain可用工具列表
#     :param mcp_base_url: http://127.0.0.1:8090
#     :return: mcp客户端，工具列表
#     """
#     client = StreamableMcpClient(mcp_base_url)
#     client.initialize()
#     resp = client.list_tools()
#     raw_tools = resp.get("result", {}).get("tools", [])

#     tool_list: list[BaseTool] = []
#     for item in raw_tools:
#         tool_name = item["name"]
#         tool_desc = item.get("description", "")
#         input_schema = item.get("inputSchema", {})

#         # 动态由json schema生成pydantic模型
#         dynamic_model = BaseModel.model_validate_json_schema(input_schema, title=tool_name)

#         tool = McpLangTool(
#             name=tool_name,
#             description=tool_desc,
#             args_schema=dynamic_model,
#             _mcp_client=client
#         )
#         tool_list.append(tool)
#         print(f"✅加载MCP工具: {tool_name}")

#     return client, tool_list

# async def get_mcp_tools():
#     # client = MultiServerMCPClient(
#     #     {
#     #         "nacos-mcp-tool-server": {
#     #             "url": "http://127.0.0.1:8090/mcp",
#     #             "transport": "sse",
#     #             "timeout": 20.0
#     #         }
#     #     }
#     # )

#     sync_tools = []
#     try:
#         mcp_server_url = "http://127.0.0.1:8090"
#         mcp_cli, tools = build_mcp_lang_tools(mcp_server_url)
#         print(f"✅ mcp 连接成功，共加载 {len(tools)} 个 MCP 工具\n")
#         return tools
#     except ExceptionGroup as eg:
#         print("===== TaskGroup inner real exceptions =====")
#         for sub_exc in eg.exceptions:
#             print(repr(sub_exc))
#             import traceback
#             traceback.print_exception(sub_exc)
#     except Exception as e:
#         print(f"获取 MCP 工具失败: {str(e)}")
#         return []
#     finally:
#         # 清理客户端资源
#         if mcp_cli:
#             try:
#                 # MultiServerMCPClient 没有 close 方法，我们依赖其垃圾回收
#                 pass
#             except Exception as e:
#                 print(f"清理 MCP 客户端时出错: {str(e)}")

# # 全局捕获底层 TaskGroup 虚假报错
# def handle_async_exception(loop, context):
#     msg = context.get("exception", context["message"])
#     if "unhandled errors in a TaskGroup" in str(msg):
#         return
#     print(f"Async Exception: {msg}")