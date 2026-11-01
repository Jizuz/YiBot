import os

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

load_dotenv()

# 进程级计量 handler：覆盖全部经 chat_model() 构造的 LLM 调用点
# （supervisor / 子 Agent / 记忆折叠 / 路由，spec: llm-token-quota）
from model.callbacks.token_meter import TokenMeterCallbackHandler  # noqa: E402

_TOKEN_METER = TokenMeterCallbackHandler()


def chat_model(callbacks: list | None = None) -> ChatOpenAI:
    model = ChatOpenAI(
        base_url=os.getenv("GLM_BASE_URL"),
        model=os.getenv("GLM_MODEL_ID"),
        api_key=os.getenv("GLM_API_KEY"),
        temperature=0.7,
        callbacks=callbacks if callbacks is not None else [_TOKEN_METER],
    )

    return model