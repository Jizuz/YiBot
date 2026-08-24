import os
import threading
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base

load_dotenv()

# 创建数据库引擎
engine = create_engine(
    (os.getenv("DATABASE_URI")),
    echo=True, # 输出SQL日志，便于调试增删改语句
    pool_size=8, # 连接池活跃连接数，适配并发操作
    max_overflow=15 # 额外允许的临时连接数
)

# 创建数据库会话
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# 声明基类
Base = declarative_base()

# 异步装饰器
def async_db_save(func):
    def wrapper(*args, **kwargs):
        threading.Thread(target=func, args=args, kwargs=kwargs, daemon=True).start()
    return wrapper