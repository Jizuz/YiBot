"""T2：高德 Key 出库测试（spec: security-hardening）。

红阶段断言口径：
- Key 从环境变量 AMAP_KEY 读取，并体现在请求 URL 中；
- 未配置 AMAP_KEY 时不发起外部请求，返回明确的未配置提示；
- 源码文件中不得残留 32 位十六进制形态的硬编码 Key（防回归）。
"""

import asyncio
from pathlib import Path
from unittest import mock

from tools import mcp_tool

MODULE_SOURCE = Path(mcp_tool.__file__).read_text(encoding="utf-8")


def test_source_has_no_hardcoded_amap_key():
    import re

    hex_keys = re.findall(r"[0-9a-f]{32}", MODULE_SOURCE)
    assert not hex_keys, f"源码疑似残留硬编码 Key: {hex_keys}"


def test_weather_uses_key_from_env(monkeypatch):
    monkeypatch.setenv("AMAP_KEY", "env-key-123")

    captured = {}

    def fake_get(url, timeout):
        captured["url"] = url
        response = mock.Mock()
        response.json.return_value = {
            "status": "1",
            "lives": [
                {
                    "city": "北京市",
                    "temperature": "18",
                    "weather": "多云",
                    "winddirection": "北",
                    "windpower": "3",
                    "humidity": "45",
                }
            ],
        }
        return response

    with mock.patch.object(mcp_tool.requests, "get", side_effect=fake_get):
        result = asyncio.run(mcp_tool.get_weather("北京"))

    assert "env-key-123" in captured["url"], captured
    assert "restapi.amap.com" in captured["url"]
    assert "北京市" in result


def test_weather_without_key_reports_missing_config(monkeypatch):
    monkeypatch.delenv("AMAP_KEY", raising=False)

    with mock.patch.object(mcp_tool.requests, "get") as fake_get:
        result = asyncio.run(mcp_tool.get_weather("北京"))

    fake_get.assert_not_called()
    assert "未配置" in result
