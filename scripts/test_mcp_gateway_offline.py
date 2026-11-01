#!/usr/bin/env python3
"""MCP 网关离线自测(无需 Nacos / MCP Server / MySQL): 验证订阅 diff / 熔断 / LB / 工具收敛 / 校验 / 重试。

运行: python scripts/test_mcp_gateway_offline.py
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

import tools.mcp_gateway.gateway as gw_mod            # noqa: E402
import tools.mcp_gateway.nacos_subscriber as sub_mod  # noqa: E402
from tools.mcp_gateway import mcp_gateway             # noqa: E402
from tools.mcp_gateway.gateway import (               # noqa: E402
    McpCallError,
    McpToolNotFoundError,
    McpValidationError,
)

# ==================== 模拟数据 ====================

SIM_INSTANCES = [
    {"ip": "10.0.0.1", "port": 8090, "instanceId": "inst-1", "healthy": True},
    {"ip": "10.0.0.2", "port": 8090, "instanceId": "inst-2", "healthy": True},
    {"ip": "10.0.0.3", "port": 8090, "instanceId": "inst-3", "healthy": True},
]


def _tool(name, desc, schema):
    return type("SimTool", (), {"name": name, "description": desc, "inputSchema": schema})()


SIM_TOOLS = [
    _tool("queryWeather", "查天气",
          {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}),
    _tool("get_right_list", "查权益",
          {"type": "object", "properties": {"userId": {"type": "integer"}}, "required": ["userId"]}),
]

FAIL_IPS: set[str] = set()   # 模拟调用失败的实例


class FakeSseClient:
    """替身: 记录建连 URL；FAIL_IPS 中的实例建连即失败。"""
    connected_urls: list[str] = []

    def __init__(self, sse_url=None, headers=None):
        self.url = sse_url

    async def __aenter__(self):
        ip = self.url.split("//")[1].split(":")[0]
        if ip in FAIL_IPS:
            raise ConnectionError(f"模拟实例宕机: {ip}")
        FakeSseClient.connected_urls.append(self.url)
        return self

    async def __aexit__(self, *args):
        return False

    async def list_tools(self):
        return SIM_TOOLS

    async def call_tool(self, name, arguments):
        return f"OK:{name}:{arguments}"


async def fake_fetch(self):
    return [dict(i) for i in SIM_INSTANCES]


# ==================== 补丁 ====================

gw_mod.SseMcpClient = FakeSseClient                   # 网关调用走替身
sub_mod.NacosSubscriber.fetch_instances = fake_fetch  # 订阅拉取走替身
mcp_gateway.registry.cooldown = 0.05                  # 加速熔断冷却(测试用)

PASSED = 0


def check(cond, msg):
    global PASSED
    assert cond, f"❌ {msg}"
    PASSED += 1
    print(f"✅ {msg}")


# ==================== 测试用例 ====================

def test_registry_sync_and_round_robin():
    added, removed = mcp_gateway.registry.sync_from_nacos(SIM_INSTANCES)
    check(added == ["inst-1", "inst-2", "inst-3"] and not removed, "注册表同步: 3 实例加入")
    picks = [mcp_gateway.registry.pick().key for _ in range(6)]
    check(picks == ["inst-1", "inst-2", "inst-3"] * 2, f"round_robin 轮转均匀: {picks}")


async def test_tool_convergence():
    for _ in range(3):  # LB 轮转从 3 个实例各拉一次
        ok = await mcp_gateway._load_tools()
        assert ok
    names = [d["name"] for d in await mcp_gateway.list_tools()]
    check(sorted(names) == ["get_right_list", "queryWeather"], f"工具收敛去重: {names}")
    spec = mcp_gateway.tools.get("queryWeather")
    check(spec.providers == {"inst-1", "inst-2", "inst-3"}, f"同名工具合并 providers: {sorted(spec.providers)}")
    check(not spec.schema_conflict, "多实例 schema 一致无冲突")


async def test_validation_fail_fast():
    before = len(FakeSseClient.connected_urls)
    try:
        await mcp_gateway.call_tool("no_such_tool", {})
        raise AssertionError("应抛 McpToolNotFoundError")
    except McpToolNotFoundError:
        pass
    try:
        await mcp_gateway.call_tool("get_right_list", {"userId": "abc"})
        raise AssertionError("应抛 McpValidationError")
    except McpValidationError:
        pass
    check(len(FakeSseClient.connected_urls) == before, "未知工具/参数非法被本地拦截, 未发起网络调用")
    check(mcp_gateway._stats["validate_blocked"] == 2, "校验拦截计数=2")


async def test_lb_routing():
    FakeSseClient.connected_urls.clear()
    for _ in range(3):
        text = await mcp_gateway.call_tool("queryWeather", {"city": "北京"})
        assert text.startswith("OK:queryWeather")
    urls = {u.split("//")[1].split(":")[0] for u in FakeSseClient.connected_urls}
    check(urls == {"10.0.0.1", "10.0.0.2", "10.0.0.3"}, f"3 次调用轮转打到 3 个实例: {sorted(urls)}")


async def test_retry_and_breaker():
    # inst-1 模拟宕机: 首次打到它失败 -> 重试换实例成功
    FakeSseClient.connected_urls.clear()
    FAIL_IPS.add("10.0.0.1")
    text = await mcp_gateway.call_tool("queryWeather", {"city": "北京"})
    check(text.startswith("OK:queryWeather"), "失败自动换实例重试成功")
    # 连续失败驱动熔断(阈值 3): 反复调用直至 inst-1 open
    for _ in range(8):
        try:
            await mcp_gateway.call_tool("queryWeather", {"city": "北京"})
        except McpCallError:
            pass
    snap = {s["instance"]: s for s in mcp_gateway.registry.snapshot()}
    check(snap["10.0.0.1:8090"]["breaker"] in ("open", "half_open"),
          f"inst-1 连续失败后熔断: {snap['10.0.0.1:8090']['breaker']}")
    # 恢复 + 冷却到半开探测 -> 成功回 closed
    FAIL_IPS.clear()
    await asyncio.sleep(0.06)
    await mcp_gateway.call_tool("queryWeather", {"city": "北京"})
    snap = {s["instance"]: s for s in mcp_gateway.registry.snapshot()}
    check(snap["10.0.0.1:8090"]["breaker"] == "closed", "半开探测成功后熔断恢复")


async def test_all_down():
    FAIL_IPS.update({"10.0.0.1", "10.0.0.2", "10.0.0.3"})
    try:
        await mcp_gateway.call_tool("queryWeather", {"city": "北京"})
        raise AssertionError("应抛 McpCallError")
    except McpCallError:
        check(mcp_gateway._stats["call_failure"] >= 1, "全部实例失败 -> 重试耗尽抛 McpCallError")
    finally:
        FAIL_IPS.clear()
    # 复位所有熔断状态
    for s in mcp_gateway.registry._instances.values():
        mcp_gateway.registry.report_success(s)


async def test_subscriber_diff():
    events: list[tuple[int, int]] = []

    async def on_change(added, removed):
        events.append((len(added), len(removed)))

    sub = sub_mod.NacosSubscriber(interval=60, on_change=on_change)
    await sub.refresh_once()
    check(sub.status()["instances"] == ["10.0.0.1:8090", "10.0.0.2:8090", "10.0.0.3:8090"],
          "订阅快照初始化")
    SIM_INSTANCES[:] = [i for i in SIM_INSTANCES if i["ip"] != "10.0.0.2"]  # 2 号下线
    await sub.refresh_once()
    check(events and events[-1] == (0, 1), f"实例下线触发 diff 回调: {events[-1] if events else None}")


async def main():
    test_registry_sync_and_round_robin()
    await test_tool_convergence()
    await test_validation_fail_fast()
    await test_lb_routing()
    await test_retry_and_breaker()
    await test_all_down()
    await test_subscriber_diff()
    stats = mcp_gateway.stats()
    print("\n========== 网关观测 ==========")
    print("calls:", stats["calls"])
    for s in stats["instances"]:
        print("instance:", s)
    print(f"\n🎉 全部 {PASSED} 项断言通过")


if __name__ == "__main__":
    asyncio.run(main())
