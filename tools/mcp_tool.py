import logging
import os

import requests
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field
logger = logging.getLogger("tools.mcp_tool")

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
    # 高德开放平台 Key：从环境变量读取（轮换后的新 Key 配置在 .env 的 AMAP_KEY）
    amap_key = os.getenv("AMAP_KEY")
    if not amap_key:
        return "❌ 天气服务未配置（缺少 AMAP_KEY），请联系管理员"

    # 高德天气基础查询接口
    url = f"https://restapi.amap.com/v3/weather/weatherInfo?city={city}&key={amap_key}&extensions=base"

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        # 校验接口返回状态
        if data.get("status") != "1" or not data.get("lives"):
            return f"查询{city}天气失败，请稍后重试"

        live_data = data["lives"][0]

        logger.info("city_weather execute!")

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


if __name__ == "__main__":
    mcp.run(transport="stdio")