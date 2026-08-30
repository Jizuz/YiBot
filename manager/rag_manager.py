import time
import os

from fastapi import File, Form, UploadFile

from rag.chroma.chroma_client import chroma_db
from rag.chroma.metadata import exist_doc, delete_doc, generate_id
from rag.core.bm25_search import bm25_engine
from rag.loader.file_loader import load_file
from rag.loader.web_loader import load_web

async def upload(file: UploadFile = File(...), desc: str = Form("")):
    """上传文件"""
    # 保存临时文件
    file_path = f"./data/{file.filename}"
    with open(file_path, "wb") as f:
        f.write(await file.read())

    # 去重更新
    if exist_doc(file.filename):
        delete_doc(file.filename)

    # 解析切片
    content = load_file(file_path)
    if not content:
        return {"code": 400, "msg": "文件解析失败", "data": {}}

    chunks = split_text(content, os.getenv("CHUNK_SIZE"), os.getenv("CHUNK_OVERLAP"))
    if not chunks:
        return {"code": 400, "msg": "文件切分失败", "data": {}}

    doc_id = generate_id()
    meta_list = [{
        "doc_id": doc_id,
        "source_name": file.filename,
        "source_type": "local",
        "desc": desc,
        "update_time": time.strftime("%Y-%m-%d %H:%M:%S")
    } for _ in chunks]

    # 批量入库
    chroma_db.add_texts(texts=chunks, metadatas=meta_list)

    bm25_engine.init_index(chunks, meta_list)

    return {"code": 200, "msg": "入库成功", "data": {"chunk_num": len(chunks), "doc_id": doc_id}}

async def save_web_info(url: str):
    """存储网页内容"""
    # 加载网页内容
    content = load_web(url)
    if not content:
        return {"code": 400, "msg": "网页解析失败，请检查URL是否正确、网络是否正常", "data": {}}

    # 去重更新
    if exist_doc(url):
        delete_doc(url)

    chunks = split_text(content, int(os.getenv("CHUNK_SIZE")), int(os.getenv("CHUNK_OVERLAP")))
    if not chunks:
        return {"code": 400, "msg": "文件切分失败", "data": {}}
    print(f"==========> web_save split chunks before length: {len(chunks)}, chunks_info: {chunks}")
    chunks = chunks[:10]
    print(f"==========> web_save split chunks after length: {len(chunks)}, chunks_info: {chunks}")

    doc_id = generate_id()
    meta_list = [{
        "doc_id": doc_id,
        "source_name": url,
        "source_type": "web",
        "update_time": time.strftime("%Y-%m-%d %H:%M:%S")
    } for _ in chunks]

    chroma_db.add_texts(texts=chunks, metadatas=meta_list)
    
    bm25_engine.init_index(chunks, meta_list)

    return {"code": 200, "msg": "网页入库成功", "data": {"doc_id": doc_id}}

async def delete(docId: str):
    """删除文档向量"""
    chroma_db.delete(where={"doc_id": docId})
    return {"code": 200, "msg": "删除成功", "data": {}}

def split_text(text: str, chunk_size: int = 512, chunk_overlap: int = 64) -> list[str]:
    """滑动窗口文本切片"""
    # 文本总长
    text_length = len(text)

    chunks = []

    start = 0
    while start < text_length:
        # 确定边界，避免越界
        end = min(start + chunk_size, text_length)
        current_chunk = text[start:end]
        chunks.append(current_chunk.strip())
        start = start + (chunk_size - chunk_overlap) # 增加重叠部分

    return chunks