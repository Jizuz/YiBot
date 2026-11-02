"""全局 LLM token 计量（T16，spec: llm-token-quota / design: 方案选型#5）。

- 挂全局 CallbackHandler：一处覆盖 supervisor / 子 Agent / 记忆折叠 / 路由
  等全部 LLM 调用点（不在各 Agent 零散实现）；
- on_llm_end 从 response.llm_output.token_usage 累计；
- 入账失败不影响主流程（仅 ERROR 日志，spec 异常场景）；
- 身份（uid）从 contextvar 读取（api/chat 入口 set），无身份调用（如脚本）跳过；
- **跨 loop 安全（e2e 修复）**：LangChain 在线程池中以同步方式回调本方法时，
  禁止 asyncio.run 新建 loop 触碰共享 redis 客户端（会污染连接池）；
  线程上下文一律入线程安全队列，由主 loop 常驻 worker（start_meter_worker，
  lifespan 启动）消费入账。
"""

import asyncio
import logging
import queue

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from core import quota
from core.quota import reset_meter_uid, set_meter_uid  # noqa: F401 - 身份 API 转出
from data.redis_client import get_redis

logger = logging.getLogger("yibot.model.token_meter")

__all__ = [
    "TokenMeterCallbackHandler",
    "set_meter_uid",
    "reset_meter_uid",
    "start_meter_worker",
]

# 进程级线程安全入账队列（同步回调线程 → 主 loop worker）
_pending_records: "queue.Queue[tuple[object, int]]" = queue.Queue()
_worker_task: asyncio.Task | None = None


class TokenMeterCallbackHandler(BaseCallbackHandler):
    """LLM 用量计量（同步回调兼容 async 运行环境，跨 loop 安全）。"""

    def __init__(self, redis=None):
        self._redis = redis

    def _redis_client(self):
        return self._redis or get_redis()

    def pending_queue(self) -> "queue.Queue[tuple[object, int]]":
        """暴露内部队列（测试与 worker 共用）。"""
        return _pending_records

    async def drain_once(self) -> int:
        """消费队列中全部待入账记录（单次，供 worker 循环与测试使用）。"""
        count = 0
        while True:
            try:
                uid, tokens = _pending_records.get_nowait()
            except queue.Empty:
                return count
            try:
                await quota.record(self._redis_client(), uid, tokens)
                count += 1
            except Exception as exc:  # noqa: BLE001 - 计量失败不影响主流程
                logger.error(
                    "token meter record failed: %s",
                    exc,
                    extra={"event": "quota_record_fail", "uid": str(uid)},
                )

    def on_llm_end(self, response: LLMResult, **kwargs) -> None:
        usage = (getattr(response, "llm_output", None) or {}).get("token_usage")
        if not usage:
            return
        total = usage.get("total_tokens") or (
            (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
        )
        if not total:
            return
        uid = quota.get_meter_uid()
        if uid is None:
            return  # 无请求身份（脚本/定时任务）不入账
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # 线程/同步上下文：入队，交主 loop worker 消费（禁止新建 loop）
                _pending_records.put_nowait((uid, int(total)))
                return
            loop.create_task(quota.record(self._redis_client(), uid, int(total)))
        except Exception as exc:  # noqa: BLE001 - 计量失败不影响主流程
            logger.error(
                "token meter dispatch failed: %s",
                exc,
                extra={"event": "quota_record_fail", "uid": str(uid)},
            )


async def _meter_worker_loop() -> None:
    """常驻消费循环（挂主事件循环，lifespan 启动）。"""
    meter = TokenMeterCallbackHandler()
    while True:
        await asyncio.sleep(0.2)
        if not _pending_records.empty():
            await meter.drain_once()


def start_meter_worker() -> None:
    """在当前事件循环启动常驻计量 worker（幂等，lifespan 调用）。"""
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
        _worker_task = loop.create_task(_meter_worker_loop())
    except RuntimeError:
        pass  # 无运行循环（脚本场景）：无 worker，队列不消费（可接受）


def stop_meter_worker() -> None:
    global _worker_task
    if _worker_task is not None:
        _worker_task.cancel()
        _worker_task = None
