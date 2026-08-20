from langchain.tools import tool
from langchain.agents.middleware import wrap_tool_call
from langchain_core.messages import ToolMessage

@tool
def get_weather(location: str) -> str:
    """查询城市天气数据
    Args:
        location: 城市
    """
    # 模拟
    weather_data = {
        "杭州": "晴，25°C，湿度60%",
        "北京": "多云，18°C，湿度 45%",
        "上海": "小雨，22°C，湿度 80%"
    }
    return weather_data.get(location, f"未找到 {location} 的天气数据")

@tool
def calculate(expression: str) -> str:
    """执行数学计算，支持加减乘除基本运算
    Args:
        expression: 计算表达式，例如 3 * 7 +2
    """
    try:
        # 安全地计算数学表达式
        result = eval(expression, {"__builtins__": {}}, {})
        return f"计算结果 {expression} = {result}"
    except Exception as e:
        return f"计算错误: {e}"

@tool
def get_time(city: str) -> str:
    """查询指定城市当前时间

    Args:
        city: 城市

    Returns:
        城市当前时间
    """
    time_data = {
        "杭州": "14:30",
        "北京": "14:30",
        "纽约": "02:30",
    }
    return time_data.get(city, f"未找到 {city} 的时间数据")


@wrap_tool_call
def handle_tool_errors(request, handler):
    """使用自定义消息处理工具执行错误。"""
    try:
        return handler(request)
    except Exception as e:
        # 向模型返回自定义错误消息
        return ToolMessage(
            content=f"工具错误：请检查您的输入并重试。({str(e)})",
            tool_call_id=request.tool_call["id"]
        )