"""编排层模型供给（T19：消除模块级 chat_model() 单例）。

- get_orch_model()：返回懒代理，属性访问时才解析当前实例——
  生产默认 chat_model()（已挂全局 token 计量），测试经 set_model 注入 mock；
- 各 Agent 文件统一 `model = get_orch_model()`，node 函数体零改动。
"""

from llm.base_llm import chat_model

_instances: dict[str, object] = {}


def get_model(name: str = "default"):
    if name not in _instances:
        _instances[name] = chat_model()
    return _instances[name]


def set_model(instance, name: str = "default") -> None:
    """注入模型实例（单测 mock 用；langgraph node 闭包经代理自动生效）。"""
    _instances[name] = instance


def reset_models() -> None:
    _instances.clear()


class _LazyModelProxy:
    """属性级懒代理：每次访问解析最新实例，保证注入即时生效。"""

    def __init__(self, name: str = "default"):
        object.__setattr__(self, "_name", name)

    def __getattr__(self, attr: str):
        return getattr(get_model(object.__getattribute__(self, "_name")), attr)


def get_orch_model(name: str = "default") -> _LazyModelProxy:
    return _LazyModelProxy(name)
