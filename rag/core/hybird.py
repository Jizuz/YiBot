# retrieval/hybrid.py
import os
import jieba
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings
from langchain_community.retrievers import BM25Retriever
from langchain_classic.retrievers import EnsembleRetriever
from langchain_core.documents import Document

from rag.chroma.chroma_client import embdding, chroma_db

class HybridRetriever:
    """Chroma 向量召回 + BM25 关键词召回。"""

    def __init__(self, chroma_path: str, collection_name: str = "medical_knowledge"):
        self.embeddings = embdding
        self.vectorstore = chroma_db
        self.ensemble = self._build_ensemble()

    def _build_ensemble(self) -> EnsembleRetriever:
        chroma_retriever = self.vectorstore.as_retriever(
            search_type="similarity",
            search_kwargs={"k": 5},
        )

        data = self.vectorstore.get(include=["documents", "metadatas"])
        docs = data["documents"] or []
        metas = data["metadatas"] or []

        if not docs:
            return EnsembleRetriever(retrievers=[chroma_retriever], weights=[1.0])

        documents = [
            Document(page_content=d, metadata=m or {})
            for d, m in zip(docs, metas)
        ]

        bm25_retriever = BM25Retriever.from_documents(
            documents=documents,
            k=5,
            preprocess_func=lambda x: list(jieba.cut(x)),  # 中文分词，关键
        )

        return EnsembleRetriever(
            retrievers=[chroma_retriever, bm25_retriever],
            weights=[0.6, 0.4],
        )

    def retrieve(self, query: str, k: int = 5) -> list[Document]:
        results = self.ensemble.invoke(query)
        return results[:k]  # 硬截断，防止 prompt 爆 token


def format_retrieved_context(docs: list[Document]) -> str:
    if not docs:
        return "（未检索到相关资料，请谨慎回答，不确定时建议就医）"
    lines = []
    for i, d in enumerate(docs, 1):
        src = d.metadata.get("source", "未知")
        lines.append(f"[{i}] 来源：{src}\n{d.page_content}")
    return "\n\n".join(lines)