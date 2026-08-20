import os
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

load_dotenv()

def chat_model() -> ChatOpenAI:
    model = ChatOpenAI(
        base_url =  os.getenv("GLM_BASE_URL"),
        model = os.getenv("GLM_MODEL_ID"),
        api_key = os.getenv("GLM_API_KEY"),
        temperature=0.7
    )

    return model