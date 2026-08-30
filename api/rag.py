from fastapi import APIRouter, File, UploadFile

from manager import rag_manager
from rag.core.hybird_search import hybird_search
from schemas.rag_schema import WebSaveBody, RagSearchBody

router = APIRouter(prefix="/rag", tags=["RAG管理"])

@router.post("/save/file")
async def upload_file(file: UploadFile = File(...)):
    """上传文档落向量库"""
    return await rag_manager.upload(file)

@router.post("/save/web")
async def upload_file(body: WebSaveBody):
    """上传文档落向量库"""
    return await rag_manager.save_web_info(body.url)

@router.post("/delete")
async def delete_doc(docId: str):
    """删除文件"""
    return await rag_manager.delete(docId)

@router.post("/search")
async def search(body: RagSearchBody):
    """Rag文档搜索"""
    return hybird_search(body.query, body.limit)