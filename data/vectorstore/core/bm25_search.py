from rank_bm25 import BM25Okapi
import jieba

class BM25Search:
    def __init__(self):
        self.corpus = []
        self.bm25 = None
        self.doc_meta = []

    def init_index(self, chunk_list: list, meta_list: list):
        self.corpus = [list(jieba.cut(text)) for text in chunk_list]
        self.bm25 = BM25Okapi(self.corpus)
        self.doc_meta = meta_list

    def search(self, query: str, top_k: int = 5):
        if not self.bm25:
            return []
        tokenized_query = list(jieba.cut(query))
        scores = self.bm25.get_scores(tokenized_query)
        ranked_idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]
        res = []
        for idx in ranked_idx:
            res.append({
                "content": self.doc_meta[idx]["content"],
                "score": float(scores[idx]),
                "meta": self.doc_meta[idx]
            })
        return res

# 全局单例
bm25_engine = BM25Search()