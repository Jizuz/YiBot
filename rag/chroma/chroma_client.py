import os

from dotenv import load_dotenv
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings

load_dotenv()

# 使用 OpenAI 向量嵌入
embdding = OpenAIEmbeddings(
    model=os.getenv("DASHSCOPE_MODEL_ID"),
    base_url=os.getenv("DASHSCOPE_BASE_URL"),
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    check_embedding_ctx_length=False,
)

# 全局Chroma实例
chroma_db = Chroma(
    collection_name="rag_knowledge",
    persist_directory=os.getenv("CHROMA_PERSIST_PATH"),
    embedding_function=embdding
)