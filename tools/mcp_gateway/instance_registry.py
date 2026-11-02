"""实例注册表：Nacos 快照 -> 实例状态(熔断/在途计数) + 多策略负载均衡(技改 §4.4 工具层)。

熔断状态机: closed(正常) --连续失败达阈值--> open(熔断，拒绝选择)
            open --冷却期到--> half_open(允许探测一次) --成功--> closed / --失败--> open
"""
import logging
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

from tools.mcp_gateway.config import (
    MCP_CIRCUIT_COOLDOWN,
    MCP_CIRCUIT_FAIL_THRESHOLD,
    MCP_LB_POLICY,
)
from tools.mcp_gateway.nacos_subscriber import instance_key

logger = logging.getLogger("yibot.mcp.registry")


@dataclass
class InstanceState:
    """单个 MCP Server 实例的运行时状态。"""
    key: str
    ip: str
    port: int
    instance_id: str
    consecutive_failures: int = 0
    breaker_opened_at: float | None = None   # 熔断打开时间(monotonic)，None=closed
    probing: bool = False                    # half_open 状态下是否已有探测在途
    in_flight: int = 0                       # 当前在途请求数(least_conn 策略用)
    last_used_at: float = 0.0
    total_calls: int = 0
    total_failures: int = 0

    def breaker_state(self, now: float, cooldown: float) -> str:
        if self.breaker_opened_at is None:
            return "closed"
        return "half_open" if now - self.breaker_opened_at >= cooldown else "open"

    def available(self, now: float, cooldown: float) -> bool:
        """closed 恒可用；half_open 且无在途探测时可探测；open 不可用。"""
        state = self.breaker_state(now, cooldown)
        if state == "closed":
            return True
        if state == "half_open":
            return not self.probing
        return False


class InstanceRegistry:
    """实例注册表：与 Nacos 快照同步 + 熔断 + 负载均衡选择。"""

    def __init__(
        self,
        policy: str = MCP_LB_POLICY,
        fail_threshold: int = MCP_CIRCUIT_FAIL_THRESHOLD,
        cooldown: float = MCP_CIRCUIT_COOLDOWN,
    ):
        self.policy = policy
        self.fail_threshold = max(1, fail_threshold)
        self.cooldown = max(0.1, cooldown)
        self._instances: dict[str, InstanceState] = {}
        self._rr_cursor = 0  # round_robin 轮转游标

    # ==================== 与 Nacos 快照同步 ====================

    def sync_from_nacos(self, instances: list[dict]) -> tuple[list[str], list[str]]:
        """用 Nacos 快照全量对齐注册表，保留幸存实例的熔断/统计状态。

        :return: (新增的实例 key 列表, 移除的实例 key 列表)
        """
        new_keys = {instance_key(i) for i in instances}
        added = sorted(k for k in new_keys if k not in self._instances)
        removed = sorted(k for k in list(self._instances) if k not in new_keys)
        for k in removed:
            state = self._instances.pop(k)
            logger.info("实例移出注册表: %s (累计调用 %d 次/失败 %d 次)",
                        k, state.total_calls, state.total_failures)

        for inst in instances:
            k = instance_key(inst)
            if k in self._instances:
                # 实例可能重启但标识复用，刷新 ip/port
                self._instances[k].ip = str(inst["ip"])
                self._instances[k].port = int(inst["port"])
                continue
            self._instances[k] = InstanceState(
                key=k, ip=str(inst["ip"]), port=int(inst["port"]),
                instance_id=str(inst.get("instanceId") or k),
            )
            logger.info("实例加入注册表: %s", k)
        return added, removed

    # ==================== 负载均衡选择 ====================

    def pick(self, exclude: set[str] | None = None) -> InstanceState | None:
        """挑选一个可用实例: 优先探测冷却到期的半开实例(加速恢复)，否则按策略从 closed 中选择。

        全部 open(且无可探测半开)时返回 None。
        :param exclude: 重试场景下需跳过的实例 key 集合(刚失败过的实例)
        """
        excluded = exclude or set()
        now = time.monotonic()
        closed = [s for s in self._instances.values()
                  if s.key not in excluded
                  and s.breaker_state(now, self.cooldown) == "closed"]
        # 严格半开: 冷却已到且无在途探测(available() 对 closed 也返回 True，不可直接用)
        half_open = [s for s in self._instances.values()
                     if s.key not in excluded
                     and s.breaker_state(now, self.cooldown) == "half_open"
                     and not s.probing]

        if not closed and not half_open:
            return None

        # 优先探测半开实例(冷却已到、无在途探测): 给恢复中的实例尽快回流的机会，
        # 避免其因永远排不到流量而滞留在 half_open 直到其它实例全部熔断
        if half_open:
            chosen = min(half_open, key=lambda s: s.key)
            chosen.probing = True  # 占住探测名额，避免半开被并发打爆
            return chosen

        candidates = closed

        if self.policy == "random":
            chosen = random.choice(candidates)
        elif self.policy == "least_conn":
            chosen = min(candidates, key=lambda s: (s.in_flight, s.last_used_at))
        else:  # round_robin
            ordered = sorted(candidates, key=lambda s: s.key)
            if self._rr_cursor >= len(ordered):
                self._rr_cursor = 0
            chosen = ordered[self._rr_cursor]
            self._rr_cursor = (self._rr_cursor + 1) % max(1, len(ordered))

        if chosen.breaker_state(now, self.cooldown) == "half_open":
            chosen.probing = True  # 占住探测名额，避免半开被并发打爆
        return chosen

    @asynccontextmanager
    async def track(self, inst: InstanceState):
        """在途请求跟踪：进入 +1 / 退出 -1(least_conn 与半开探测名额释放)。"""
        inst.in_flight += 1
        inst.last_used_at = time.monotonic()
        inst.total_calls += 1
        try:
            yield inst
        finally:
            inst.in_flight -= 1
            if inst.breaker_state(time.monotonic(), self.cooldown) == "half_open":
                inst.probing = False

    # ==================== 成败上报(驱动熔断) ====================

    def report_success(self, inst: InstanceState) -> None:
        if inst.breaker_opened_at is not None:
            logger.info("实例 %s 半开探测成功，熔断恢复(closed)", inst.key)
        inst.consecutive_failures = 0
        inst.breaker_opened_at = None
        inst.probing = False

    def report_failure(self, inst: InstanceState) -> None:
        inst.total_failures += 1
        inst.consecutive_failures += 1
        if inst.breaker_opened_at is None and inst.consecutive_failures >= self.fail_threshold:
            inst.breaker_opened_at = time.monotonic()
            inst.probing = False
            logger.warning("实例 %s 连续失败 %d 次，熔断打开(open)，冷却 %.0fs",
                           inst.key, inst.consecutive_failures, self.cooldown)
        elif inst.breaker_opened_at is not None:
            # 半开探测失败：重新计时熔断
            inst.breaker_opened_at = time.monotonic()
            inst.probing = False
            logger.warning("实例 %s 半开探测失败，重新熔断(open)", inst.key)

    # ==================== 观测 ====================

    def snapshot(self) -> list[dict]:
        now = time.monotonic()
        return [
            {
                "instance": f"{s.ip}:{s.port}",
                "breaker": s.breaker_state(now, self.cooldown),
                "consecutive_failures": s.consecutive_failures,
                "in_flight": s.in_flight,
                "total_calls": s.total_calls,
                "total_failures": s.total_failures,
            }
            for s in sorted(self._instances.values(), key=lambda x: x.key)
        ]

    def healthy_count(self) -> int:
        return sum(
            1 for s in self._instances.values()
            if s.available(time.monotonic(), self.cooldown)
        )
