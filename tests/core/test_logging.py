"""T5：core/logging 结构化日志 + trace_id 测试（spec: production-hardening）。"""

import json
import logging

from core.logging import (
    JsonFormatter,
    get_trace_id,
    new_trace_id,
    reset_trace_id,
    set_trace_id,
    setup_logging,
)


def test_trace_id_roundtrip():
    assert get_trace_id() is None or isinstance(get_trace_id(), str)
    trace_id = new_trace_id()
    token = set_trace_id(trace_id)
    try:
        assert get_trace_id() == trace_id
    finally:
        reset_trace_id(token)
    # reset 后不再持有该值
    assert get_trace_id() != trace_id


def test_structured_log_carries_trace_id(caplog):
    setup_logging()
    token = set_trace_id("trace-abc-123")
    try:
        logger = logging.getLogger("yibot.test")
        with caplog.at_level(logging.INFO, logger="yibot.test"):
            logger.info("hello", extra={"event": "unit_test"})
        record = caplog.records[-1]
        assert getattr(record, "trace_id") == "trace-abc-123"
        assert getattr(record, "event") == "unit_test"
    finally:
        reset_trace_id(token)


def test_log_output_is_json_with_trace_id(caplog):
    import io

    setup_logging(json_stream=True)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    from core.logging import JsonFormatter

    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("yibot.json")
    logger.addHandler(handler)
    token = set_trace_id("trace-json-1")
    try:
        logger.info("payload", extra={"uid": 42, "event": "chat"})
        payload = json.loads(stream.getvalue())
        assert payload["trace_id"] == "trace-json-1"
        assert payload["uid"] == 42
        assert payload["event"] == "chat"
        assert payload["level"] == "INFO"
    finally:
        reset_trace_id(token)
        logger.removeHandler(handler)


def test_sensitive_fields_masked_in_message(caplog):
    setup_logging()
    logger = logging.getLogger("yibot.mask")
    with caplog.at_level(logging.INFO, logger="yibot.mask"):
        logger.info("login password=SuperSecret123 ok")
        logger.info("token=eyJhbGciOi.very.long.value refreshed")
    messages = [r.getMessage() for r in caplog.records]
    assert "SuperSecret123" not in " ".join(messages)
    assert "eyJhbGciOi" not in " ".join(messages)
    assert "password=***" in messages[0]
    assert "token=***" in messages[1]
