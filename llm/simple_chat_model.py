from langchain_openai import ChatOpenAI

from llm.base_llm import chat_model

model = chat_model()

def get_ai_response(chats: str) -> str:
    result = model.invoke(chats)
    if result is None:
        return f"刚刚思想开小差了，请您再说一遍"

    return result.content