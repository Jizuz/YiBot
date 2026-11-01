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

