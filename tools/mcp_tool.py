import requests
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

mcp = FastMCP("local_mcp")

"""高德天气API返回数据：
{
    "status": "1",
    "count": "1",
    "info": "OK",
    "infocode": "10000",
    "lives": [
        {
            "province": "上海",
            "city": "上海市",
            "adcode": "310000",
            "weather": "阴",
            "temperature": "31",
            "winddirection": "北",
            "windpower": "≤3",
            "humidity": "68",
            "reporttime": "2026-08-21 14:33:07",
            "temperature_float": "31.0",
            "humidity_float": "68.0"
        }
    ]
}
"""
@mcp.tool(name="city_weather", description="查询指定城市的天气情况")
async def get_weather(city: Annotated[str, Field(description="目标城市名称（中文全称）", examples=["北京", "上海", "杭州", "深圳"])]) -> str:
    """获取指定城市天气"""
    AMAP_KEY = "f26670393f1020c4f000fc2abffd560f"

    # 高德天气基础查询接口
    url = f"https://restapi.amap.com/v3/weather/weatherInfo?city={city}&key={AMAP_KEY}&extensions=base"

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        # 校验接口返回状态
        if data.get("status") != "1" or not data.get("lives"):
            return f"查询{city}天气失败，请稍后重试"

        live_data = data["lives"][0]

        print("city_weather execute!")

        # 格式化返回
        return (
            f"📢 {live_data['city']} 实时天气 \n"
            f"🌡️ 当前温度: {live_data['temperature']} \n"
            f"☁️ 天气状况: {live_data['weather']} \n"
            f"🌬️ 风向风力: {live_data['winddirection']}风 {live_data['windpower']}级"
            f"💧 空气湿度: {live_data['humidity']}%"
        )

    except requests.exceptions.Timeout:
        return f"❌ 获取天气超时，请检查网络连接"
    except Exception as e:
        return f"❌ 获取天气异常，异常原因:{str(e)}"

# @mcp.tool(name="basic_calculate", description="执行基础数学计算，支持加减乘除基本运算")
# async def calculate(expression: Annotated[str, Field(description="数学计算表达式", examples=["1 + 2", "4 * 9 + 23"])]) -> str:
#     """执行数学计算，支持加减乘除基本运算"""
#     try:
#         # 安全地计算数学表达式
#         result = eval(expression, {"__builtins__": {}}, {})

#         print("basic_calculate execute!")

#         return (f"计算结果 {expression} = {result}")
#     except Exception as e:
#         return f"计算错误: {e}"

if __name__ == "__main__":
    mcp.run(transport="stdio")