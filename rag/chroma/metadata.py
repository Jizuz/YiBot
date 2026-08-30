from rag.chroma.chroma_client import chroma_db
import uuid

def exist_doc(source_name: str) -> bool:
    """判断文件/网页源是否已入库"""
    res = chroma_db.get(where={"source_name": source_name})
    return len(res["ids"]) > 0

def delete_doc(source_name: str):
    """删除指定数据源所有切片"""
    chroma_db.delete(where={"source_name": source_name})

def generate_id() -> str:
    return str(uuid.uuid4())

def get_all_metadata():
    """获取全量元数据（知识库列表接口用）"""
    return chroma_db.get()["metadatas"]