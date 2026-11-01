import os
from PyPDF2 import PdfReader
from docx import Document

def load_file(file_path: str) -> str:
    """加载文件，支持pdf、docx、txt、md格式"""
    ext = os.path.splitext(file_path)[-1].lower()
    content = ""
    if ext == ".pdf":
        reader = PdfReader(file_path)
        for page in reader.pages:
            content += page.extract_text() or ""
    elif ext == ".docx":
        doc = Document(file_path)
        content = "\n".join([p.text for p in doc.paragraphs])
    elif ext in [".txt", ".md"]:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
            
    return content.strip()