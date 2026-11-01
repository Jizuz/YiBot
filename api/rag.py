from fastapi import APIRouter, Depends, File, UploadFile

from api.deps.auth import get_current_user
from data.vectorstore import rag_manager
from data.vectorstore.core.hybird_search import hybird_search
from schemas.rag_schema import WebSaveBody, RagSearchBody

router = APIRouter(prefix="/rag", tags=["RAG管理"])

@router.post("/save/file")
async def upload_file(file: UploadFile = File(...), current=Depends(get_current_user)):
    """上传文档落向量库（受保护）"""
    return await rag_manager.upload(file)

@router.post("/save/web")
async def upload_file(body: WebSaveBody, current=Depends(get_current_user)):
    """上传文档落向量库（受保护）"""
    return await rag_manager.save_web_info(body.url)

@router.post("/delete")
async def delete_doc(docId: str, current=Depends(get_current_user)):
    """删除文件（受保护）"""
    return await rag_manager.delete(docId)

@router.post("/search")
async def search(body: RagSearchBody, current=Depends(get_current_user)):
    """Rag文档搜索（受保护）"""
    return hybird_search(body.query, body.limit)