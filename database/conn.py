import os
import threading
import aiomysql
from langgraph.checkpoint.mysql.aio import AIOMySQLSaver
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

async def create_checkpointer():
    """在应用启动时调用，返回一个可长期使用的 Checkpointer。"""
    pool = await aiomysql.create_pool(
        host="localhost",
        port=3306,
        user=os.environ["MYSQL_USER"],
        password=os.environ["MYSQL_PASSWORD"],
        db=os.environ["MYSQL_DB"],
        maxsize=10,
        autocommit=True,   # 必须！否则 setup() 建表不会提交 [citation:2][citation:3]
    )
    checkpointer = AIOMySQLSaver(pool)
    # await checkpointer.setup()  # 首次运行创建表
    return checkpointer, pool