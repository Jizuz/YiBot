"""依赖注入：数据库会话（T9 起统一，替换各路由自建 get_db）。"""

from database.conn import SessionLocal


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
