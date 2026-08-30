import os
from dotenv import load_dotenv

from rag.core.vector_search import vector_search
from rag.core.bm25_search import bm25_engine

load_dotenv()

VECTOR_WEIGHT = float(os.getenv("VECTOR_WEIGHT"))
BM25_WEIGHT = float(os.getenv("BM25_WEIGHT"))

def hybird_search(query: str, top_k: int = 5):
    print(f"==========> hybird search start, query: {query} ...")
    # 两路召回
    vec_res = vector_search(query, top_k=10)
    # print(f"==========> hybird vector result: {vec_res}")
    bm25_res = bm25_engine.search(query, top_k=10)
    # print(f"==========> hybird bm25 result: {vec_res}")

    # 得分归一化
    all_docs = {}
    for item in vec_res:
        all_docs[item["content"]] = {"vec_score": item["score"], "bm25_score": 0, "meta": item["meta"]}
    for item in bm25_res:
        if item["content"] in all_docs:
            all_docs[item["content"]]["bm25_score"] = item["score"]
        else:
            all_docs[item["content"]] = {"vec_score": 0, "bm25_score": item["score"], "meta": item["meta"]}

    # 加权融合
    blend_list = []
    for content, score_dict in all_docs.items():
        final_score = score_dict["vec_score"] * VECTOR_WEIGHT + score_dict["bm25_score"] * BM25_WEIGHT
        blend_list.append({
            "content": content,
            "score": final_score,
            "meta": score_dict["meta"]
        })

    # 排序
    blend_list.sort(key=lambda x: x["score"], reverse=True)
    return blend_list[:top_k]