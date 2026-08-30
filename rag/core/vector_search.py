import os
from dotenv import load_dotenv

from rag.chroma.chroma_client import chroma_db

def vector_search(query: str, top_k: int = 5):
    """向量相似度查询"""
    # !!! `.similarity_search(query,k)` → 返回 `List[Document]` ❌只有文档对象，没有分数 !!!
    # retriver = chroma_db.as_retriever(search_type="similarity", search_kwargs={"k": top_k})
    # results = retriver.invoke(query)
    results = chroma_db.similarity_search_with_relevance_scores(query, top_k)
    print(f"===========> vector_search query: {query}, result: {results}")
    if not results:
        return []

    res = []
    for doc, score in results:
        # if score < float(os.getenv("SIMILARITY_THRESHOLD")):
        #     continue
        res.append({
            "content": doc.page_content,
            "score": score,
            "meta": doc.metadata
        })
    return res

    