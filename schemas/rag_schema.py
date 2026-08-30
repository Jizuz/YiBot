from pydantic import BaseModel

class WebSaveBody(BaseModel):
    url: str

class RagSearchBody(BaseModel):
    query: str
    limit: int = 5