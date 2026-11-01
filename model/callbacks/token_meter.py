"""全局 LLM token 计量（T16，spec: llm-token-quota / design: 方案选型#5）。

- 挂全局 CallbackHandler：一处覆盖 supervisor / 子 Agent / 记忆折叠 / 路由
  等全部 LLM 调用点（不在各 Agent 零散实现）；
- on_llm_end 从 response.llm_output.token_usage 累计；
- 入账异步执行且失败不影响主流程（仅 ERROR 日志，spec 异常场景）；
- 身份（uid）从 contextvar 读取（api/chat 入口 set），无身份调用（如脚本）跳过。
"""

import asyncio
import logging

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from core import quota
from core.quota import reset_meter_uid, set_meter_uid  # noqa: F401 - 身份 API 转出
from data.redis_client import get_redis

logger = logging.getLogger("yibot.model.token_meter")

__all__ = ["TokenMeterCallbackHandler", "set_meter_uid", "reset_meter_uid"]


class TokenMeterCallbackHandler(BaseCallbackHandler):
    """LLM 用量计量（同步回调兼容 async 运行环境）。"""

    def __init__(self, redis=None):
        self._redis = redis

    def _redis_client(self):
        return self._redis or get_redis()

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
            redis_client = self._redis_client()
            coroutine = quota.record(redis_client, uid, int(total))
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(coroutine)  # 生产路径：异步入账不阻塞
            except RuntimeError:
                asyncio.run(coroutine)  # 同步上下文（测试/脚本）
        except Exception as exc:  # noqa: BLE001 - 计量失败不影响主流程
            logger.error(
                "token meter record failed: %s",
                exc,
                extra={"event": "quota_record_fail", "uid": str(uid)},
            )
