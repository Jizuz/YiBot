import logging
import requests
from bs4 import BeautifulSoup
logger = logging.getLogger("data.vectorstore.loader.web_loader")

def load_web(url: str) -> str:
    """加载网页内容"""
    logger.info(f"web_loader load url: {url}")
    try:
        resp = requests.get(url, timeout=5)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
        return soup.get_text(strip=True)
    except Exception as e:
        logger.info(f"网页加载失败, url={url}, e={e}")
        return ""