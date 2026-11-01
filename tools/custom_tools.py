import ast
import math

from langchain.tools import tool
from langchain.agents.middleware import wrap_tool_call
from langchain_core.messages import ToolMessage

# 安全求值约束（spec: security-hardening / design: 方案选型#9）
MAX_EXPRESSION_LENGTH = 100   # 表达式长度上限
MAX_EXPONENT = 1000           # 幂运算指数绝对值上限（防超大整数资源滥用）
MAX_RESULT_DIGITS = 1000      # 结果十进制位数上限

_ALLOWED_BINOPS = (
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
)
_ALLOWED_UNARYOPS = (ast.UAdd, ast.USub)


def _eval_node(node: ast.AST):
    """白名单递归求值：仅支持算术运算，白名单外节点一律拒绝。"""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BINOPS):
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if isinstance(node.op, ast.Pow):
            exponent = right
            if not isinstance(exponent, int) or abs(exponent) > MAX_EXPONENT:
                raise ValueError("指数超出允许范围")
            if abs(left) > 1 and abs(exponent) * math.log10(max(abs(left), 2)) > MAX_RESULT_DIGITS:
                raise ValueError("结果超出允许范围")
        if isinstance(node.op, ast.Div) and right == 0:
            raise ZeroDivisionError("除数不能为零")
        if isinstance(node.op, ast.FloorDiv) and right == 0:
            raise ZeroDivisionError("除数不能为零")
        if isinstance(node.op, ast.Mod) and right == 0:
            raise ZeroDivisionError("除数不能为零")
        return {
            ast.Add: lambda: left + right,
            ast.Sub: lambda: left - right,
            ast.Mult: lambda: left * right,
            ast.Div: lambda: left / right,
            ast.FloorDiv: lambda: left // right,
            ast.Mod: lambda: left % right,
            ast.Pow: lambda: left ** right,
        }[type(node.op)]()
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, _ALLOWED_UNARYOPS):
        operand = _eval_node(node.operand)
        return operand if isinstance(node.op, ast.UAdd) else -operand
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("仅支持数字字面量")
        return value
    raise ValueError("表达式包含不支持的运算")


def safe_arith(expression: str):
    """对算术表达式做白名单解析求值，返回数字；非法输入抛 ValueError/ZeroDivisionError。"""
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("表达式为空")
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise ValueError("表达式过长")
    tree = ast.parse(expression, mode="eval")  # 语法错误抛 SyntaxError
    result = _eval_node(tree)
    if isinstance(result, float) and not math.isfinite(result):
        raise ValueError("结果超出数值范围")
    if isinstance(result, int) and len(str(abs(result))) > MAX_RESULT_DIGITS:
        raise ValueError("结果超出允许范围")
    return result

@tool
def calculate(expression: str) -> str:
    """执行数学计算，支持加减乘除基本运算
    Args:
        expression: 计算表达式，例如 3 * 7 +2
    """
    try:
        result = safe_arith(expression)
        return f"计算结果 {expression} = {result}"
    except Exception as e:
        return f"计算错误: {e}"

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