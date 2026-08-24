import asyncio
from functools import wraps
from langchain_mcp_adapters.client import MultiServerMCPClient

def sync_wrapper(async_func):
    """将异步函数转换为同步函数的装饰器"""
    @wraps(async_func)
    def sync_wrapper_func(*args, **kwargs):
        return asyncio.run(async_func(*args, **kwargs))
    return sync_wrapper_func

async def get_mcp_tools():
    """获取 MCP 工具列表并转换为同步工具"""
    client = None
    try:
        client = MultiServerMCPClient(
            {
                "local_mcp": {
                    "command": "python",
                    "args": ["/Users/jizuz/WorkSpace/PYTHON/McpServer/mcp_tool.py"],
                    "transport": "stdio",
                }
            }
        )
        tools = await client.get_tools()

        # 将异步工具包装为同步工具
        sync_tools = []
        for tool in tools:
            print(f"=======>>>>> [{tool.name}]:[{tool.description}]")
            
            # 获取实际的异步执行函数
            async_func = None
            if hasattr(tool, 'coroutine') and tool.coroutine is not None:
                async_func = tool.coroutine
            elif hasattr(tool, 'arun') and callable(tool.arun):
                async_func = tool.arun
            elif hasattr(tool, '_run') and callable(tool._run):
                # _run 通常是同步的，但我们尝试包装它
                try:
                    # 先检查 _run 是否是异步的
                    import inspect
                    if inspect.iscoroutinefunction(tool._run):
                        async_func = tool._run
                    else:
                        # 如果 _run 是同步的，直接使用
                        async_func = None
                except:
                    async_func = None
            
            if async_func is None:
                print(f"警告：工具 {tool.name} 没有找到可执行的异步函数，跳过")
                continue
            
            # 创建同步版本的函数
            @sync_wrapper
            def sync_func(*args, **kwargs):
                return async_func(*args, **kwargs)
            
            sync_tool_func = sync_func
            
            # 保留原始函数的元数据
            if hasattr(async_func, '__name__'):
                sync_tool_func.__name__ = async_func.__name__
            else:
                sync_tool_func.__name__ = tool.name
                
            if hasattr(async_func, '__doc__'):
                sync_tool_func.__doc__ = async_func.__doc__
            else:
                sync_tool_func.__doc__ = tool.description
            
            # 创建同步工具对象
            sync_tool = type(tool)(
                name=tool.name,
                description=tool.description,
                func=sync_tool_func,
                args_schema=tool.args_schema,
                return_direct=tool.return_direct,
            )
            sync_tools.append(sync_tool)
            
            print(f"✓ 成功将工具 {tool.name} 转换为同步版本")

        return sync_tools
    except Exception as e:
        print(f"获取 MCP 工具失败: {str(e)}")
        return []
    finally:
        # 清理客户端资源
        if client:
            try:
                # MultiServerMCPClient 没有 close 方法，我们依赖其垃圾回收
                pass
            except Exception as e:
                print(f"清理 MCP 客户端时出错: {str(e)}")
