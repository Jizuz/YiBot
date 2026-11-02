"""T16：全局 token 计量 CallbackHandler 测试（spec: llm-token-quota）。

场景：token_usage 提取与入账、缺失字段容错、无身份跳过、入账失败不中断。
"""

import asyncio

import pytest
from langchain_core.outputs import LLMResult

from core import quota
from data.redis_client import close_redis, init_redis
from model.callbacks.token_meter import (
    TokenMeterCallbackHandler,
    reset_meter_uid,
    set_meter_uid,
)


class _FakeRunManager:
    pass


def _llm_result(total_tokens=123, prompt=50, completion=73):
    return LLMResult(
        generations=[],
        llm_output={
            "token_usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total_tokens,
            }
        },
    )


@pytest.fixture(autouse=True)
def _redis():
    client = init_redis(fake=True)
    yield client
    close_redis()


def test_meter_records_usage(_redis):
    handler = TokenMeterCallbackHandler()
    token = set_meter_uid(42)
    try:
        handler.on_llm_end(_llm_result(total_tokens=200), run_manager=_FakeRunManager())
    finally:
        reset_meter_uid(token)
    # 同步上下文：入队 → worker（drain_once）消费入账
    asyncio.run(handler.drain_once())
    assert asyncio.run(quota.get_used(_redis, uid=42)) == 200


def test_meter_tolerates_missing_token_usage(_redis):
    handler = TokenMeterCallbackHandler()
    result = LLMResult(generations=[], llm_output=None)
    handler.on_llm_end(result, run_manager=_FakeRunManager())  # 不抛异常
    assert asyncio.run(quota.get_used(_redis, uid=42)) == 0


def test_meter_skips_without_uid(_redis):
    handler = TokenMeterCallbackHandler()
    handler.on_llm_end(_llm_result(), run_manager=_FakeRunManager())
    assert asyncio.run(quota.get_used(_redis, uid=42)) == 0


def test_meter_in_thread_context_defers_to_queue(_redis):
    """同步线程上下文（LangChain executor）：不得新建 loop 触碰共享客户端，
    记账经线程安全队列由常驻 worker 消费（e2e 发现的跨 loop 池污染修复）。"""
    handler = TokenMeterCallbackHandler()
    token = set_meter_uid(55)
    try:
        handler.on_llm_end(
            LLMResult(
                generations=[],
                llm_output={"token_usage": {"total_tokens": 77}},
            ),
            run_manager=_FakeRunManager(),
        )
    finally:
        reset_meter_uid(token)

    pending = handler.pending_queue()
    assert not pending.empty(), "线程上下文必须入队而非新建 loop"
    uid, tokens = pending.get_nowait()
    assert (uid, tokens) == (55, 77)
    pending.put_nowait((uid, tokens))  # 放回供 drain 消费

    asyncio.run(handler.drain_once())
    assert asyncio.run(quota.get_used(_redis, uid=55)) == 77

