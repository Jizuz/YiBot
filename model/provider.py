"""模型工厂（T21 归位，spec: production-hardening）。

- 参数可配：temperature / timeout / max_retries 从集中配置读取（design: 模型层）；
- 进程级计量 handler：覆盖全部经 chat_model() 构造的 LLM 调用点
  （supervisor / 子 Agent / 记忆折叠 / 路由，spec: llm-token-quota）。
"""

from langchain_openai import ChatOpenAI

from config.settings import get_settings
from model.callbacks.token_meter import TokenMeterCallbackHandler

_TOKEN_METER = TokenMeterCallbackHandler()


def chat_model(callbacks: list | None = None) -> ChatOpenAI:
    settings = get_settings()
    return ChatOpenAI(
        base_url=settings.GLM_BASE_URL,
        model=settings.GLM_MODEL_ID,
        api_key=settings.GLM_API_KEY,
        temperature=settings.LLM_TEMPERATURE,
        timeout=settings.LLM_TIMEOUT_SECONDS,
        max_retries=settings.LLM_MAX_RETRIES,
        callbacks=callbacks if callbacks is not None else [_TOKEN_METER],
    )