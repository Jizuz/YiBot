"""工具收敛注册表：跨实例聚合/去重 MCP 工具定义，缓存 + TTL + 实例变更失效 + 本地参数校验(技改 §4.4)。

收敛语义:
- 多实例暴露同名工具 -> 合并为一个 ToolSpec，记录全部 provider(实例)，schema 冲突时保留首个并告警;
- 实例集合变化(上线/下线) -> 缓存立即失效，下次访问重拉;
- TTL 过期 -> 后台顺路刷新，避免每次调用都 listTools。
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, create_model

logger = logging.getLogger("yibot.mcp.tools")

_JSON_SCHEMA_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "bool": bool,
    "array": list,
    "object": dict,
}


def schema_to_args_model(tool_name: str, input_schema: dict | None) -> type[BaseModel] | None:
    """由 MCP 工具的 inputSchema(JSON Schema) 动态生成 pydantic 参数模型(用于调用前校验)。"""
    schema = input_schema or {}
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])

    fields: dict[str, tuple] = {}
    for field_name, field_def in properties.items():
        field_def = field_def or {}
        py_type = _JSON_SCHEMA_TYPE_MAP.get(str(field_def.get("type", "string")).lower(), str)
        description = str(field_def.get("description") or "")
        if field_name in required:
            fields[field_name] = (py_type, Field(..., description=description))
        else:
            fields[field_name] = (py_type, Field(default=field_def.get("default"), description=description))

    safe_name = "".join(c if c.isalnum() else "_" for c in tool_name)
    return create_model(f"McpArgs_{safe_name}", **fields) if fields else None


def _schema_fingerprint(input_schema: dict | None) -> str:
    """schema 指纹(仅用于冲突检测)。"""
    props = (input_schema or {}).get("properties") or {}
    return ",".join(sorted(f"{k}:{v.get('type')}" for k, v in props.items()))


@dataclass
class ToolSpec:
    """收敛后的单个工具定义。"""
    name: str
    description: str
    input_schema: dict
    args_model: type[BaseModel] | None = None
    providers: set[str] = field(default_factory=set)   # 提供该工具的实例 key 集合
    schema_conflict: bool = False                      # 多实例 schema 不一致标记

    def validate_arguments(self, arguments: dict | None) -> dict:
        """调用前本地校验(仅校验不改写原参数，避免静默丢弃 schema 外字段)。"""
        args = dict(arguments or {})
        if self.args_model is not None:
            self.args_model.model_validate(args)
        return args

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "providers": sorted(self.providers),
            "schema_conflict": self.schema_conflict,
        }


class ToolRegistry:
    """工具收敛注册表。"""

    def __init__(self, ttl: float):
        self._ttl = max(1.0, ttl)
        self._tools: dict[str, ToolSpec] = {}
        self._loaded_at: float = 0.0
        self._generations = 0  # 失效/重载次数(观测用)

    # ==================== 状态查询 ====================

    @property
    def empty(self) -> bool:
        return not self._tools

    @property
    def stale(self) -> bool:
        return (time.monotonic() - self._loaded_at) >= self._ttl

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def all(self) -> list[ToolSpec]:
        return sorted(self._tools.values(), key=lambda t: t.name)

    # ==================== 写入与失效 ====================

    def update(self, tools: list[Any], provider: str) -> int:
        """用某实例 listTools 结果合并进注册表(同名合并 provider，冲突保留首个)。"""
        for tool in tools:
            name = getattr(tool, "name", None)
            if not name:
                continue
            desc = (getattr(tool, "description", "") or "").strip()
            schema = getattr(tool, "inputSchema", None) or {}
            existing = self._tools.get(name)
            if existing is None:
                self._tools[name] = ToolSpec(
                    name=name,
                    description=desc or f"MCP 工具 {name}",
                    input_schema=schema,
                    args_model=schema_to_args_model(name, schema),
                    providers={provider},
                )
                continue
            # 同名工具: 合并 provider；schema 不一致保留首个并标记冲突
            existing.providers.add(provider)
            if _schema_fingerprint(schema) != _schema_fingerprint(existing.input_schema):
                existing.schema_conflict = True
                logger.warning("工具 %s 在实例 %s 与 %s 上 schema 不一致，以首个为准(冲突已标记)",
                               name, provider, sorted(existing.providers))
        self._loaded_at = time.monotonic()
        self._generations += 1
        return len(self._tools)

    def clear(self) -> None:
        """实例集合变化时失效缓存(下次访问重拉)。"""
        if self._tools:
            logger.info("工具缓存失效(实例集合变化)，下次访问重新收敛")
        self._tools.clear()
        self._loaded_at = 0.0

    def status(self) -> dict:
        return {
            "tool_count": len(self._tools),
            "tools": [t.name for t in self.all()],
            "loaded_at": self._loaded_at,
            "stale": self.stale,
            "generations": self._generations,
        }
