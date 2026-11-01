"""结构化日志 + trace_id（T5，spec: production-hardening）。

- contextvar 承载 trace_id，随请求全链路传播（请求→编排→模型→数据）；
- 自定义 LogRecord 工厂为每条记录注入 trace_id，并对敏感字段脱敏
  （password/token/secret/key=值 → ***，CR 安全红线：敏感信息不落日志）；
- JsonFormatter 输出机器可读 JSON。
"""

import contextvars
import logging
import re
import sys
import uuid

_trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trace_id", default=None
)

__all__ = [
    "new_trace_id",
    "get_trace_id",
    "set_trace_id",
    "reset_trace_id",
    "mask_sensitive",
    "JsonFormatter",
    "setup_logging",
]

# 敏感字段脱敏：password=xxx / token=xxx / secret=xxx / api_key=xxx（值 → ***）
_SENSITIVE_PATTERN = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|authorization)\s*[=:]\s*\S+"
)


def new_trace_id() -> str:
    """生成新 trace_id（uuid4 短格式）。"""
    return uuid.uuid4().hex


def get_trace_id() -> str | None:
    return _trace_id_var.get()


def set_trace_id(trace_id: str):
    """设置当前上下文 trace_id，返回可传给 reset_trace_id 的 Token。"""
    return _trace_id_var.set(trace_id)


def reset_trace_id(token) -> None:
    """恢复 trace_id 上下文（Python 3.13 移除 Token.reset，须用本函数）。"""
    _trace_id_var.reset(token)


def mask_sensitive(text: str) -> str:
    """对消息中的敏感键值对脱敏（用于日志输出前的统一处理）。"""
    return _SENSITIVE_PATTERN.sub(_mask_pair_by_match, text)


def _mask_pair_by_match(match: "re.Match[str]") -> str:
    matched, key = match.group(0), match.group(1)
    sep_match = re.search(r"[=:]", matched)
    sep = sep_match.group(0) if sep_match else "="
    return f"{key}{sep}***"


class JsonFormatter(logging.Formatter):
    """结构化 JSON 日志格式。"""

    def format(self, record: logging.LogRecord) -> str:
        import json

        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for attr in ("trace_id", "uid", "event"):
            value = getattr(record, attr, None)
            if value is not None:
                payload[attr] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_OLD_RECORD_FACTORY = logging.getLogRecordFactory()


def _record_factory(*args, **kwargs) -> logging.LogRecord:
    record = _OLD_RECORD_FACTORY(*args, **kwargs)
    trace_id = _trace_id_var.get()
    if trace_id is not None:
        record.trace_id = trace_id  # noqa: SLF001
    if isinstance(record.msg, str):
        record.msg = mask_sensitive(record.msg)
    return record


def setup_logging(json_stream: bool = True, level: int = logging.INFO) -> None:
    """初始化应用日志（幂等）：全局 Record 工厂注入 trace_id + JSON 流输出。"""
    if getattr(setup_logging, "_done", False):
        return
    logging.setLogRecordFactory(_record_factory)
    root = logging.getLogger()
    root.setLevel(level)
    if json_stream:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    setup_logging._done = True  # type: ignore[attr-defined]
