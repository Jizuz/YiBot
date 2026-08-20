from dotenv import load_dotenv
from langchain_core.tools import tool
from langchain_tavily import TavilyExtract, TavilySearch

load_dotenv()

@tool
def tavily_search(question: str, limit: int = 3) -> str:
    """搜索互联网获取最新消息，适用范围：
    - 询问最新新闻、行情、版本更新等
    - 询问知识库意外事件的通用知识
    - 需要权威资料佐证的查询

    不适用：主观问题、私域业务问题

    Args:
        question: 问题
        limit: 返回条数限制
    Returns:
        结果列表
    """
    if not question:
        raise "请告诉我你要查询的问题"

    tavily_tool = TavilySearch(max_results=limit)
    result = tavily_tool.invoke(question)

    return result

@tool
def tavily_extract(url: str) -> str:
    """提取互联网网页信息，实时获取网页内容"""
    tavily_extract = TavilyExtract()
    result = tavily_extract.invoke({"urls": url})

    return result