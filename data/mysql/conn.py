"""MySQL 连接与受控落库（T20，spec: production-hardening）。

- 同步 engine（echo=False，生产禁 SQL 日志）；
- submit_db_task：替代裸 threading.Thread——线程池受控、在途上限 1000、
  失败 ERROR 日志（禁止静默丢消息）；
- session_scope：统一会话上下文（commit/rollback/close）；
- checkpointer 连接参数全部走集中配置（去除硬编码 localhost）。
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import aiomysql
from dotenv import load_dotenv
from langgraph.checkpoint.mysql.aio import AIOMySQLSaver
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from config.settings import get_settings

load_dotenv()

logger = logging.getLogger("yibot.data.mysql")

settings = get_settings()

# 创建数据库引擎（echo=False：生产禁 SQL 日志，CR 红线）
engine = create_engine(
    settings.DATABASE_URI,
    echo=False,
    pool_size=8,       # 连接池活跃连接数，适配并发操作
    max_overflow=15,   # 额外允许的临时连接数
)

# 创建数据库会话
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# 声明基类
Base = declarative_base()

# ---- 受控后台落库（替代裸 threading.Thread） ----
_DB_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="yibot-db")
_MAX_INFLIGHT = 1000
_inflight = threading.Semaphore(_MAX_INFLIGHT)


def submit_db_task(func, *args, **kwargs):
    """提交后台落库任务：线程池受控 + 在途上限 + 失败日志。

    Returns:
        Future；队列满额时返回 None（并记录丢弃告警）。
    """
    if not _inflight.acquire(blocking=False):
        logger.error(
            "db task queue full, task dropped",
            extra={"event": "db_queue_drop", "task": getattr(func, "__name__", "?")},
        )
        return None
    future = _DB_EXECUTOR.submit(func, *args, **kwargs)

    def _done(f):
        _inflight.release()
        exc = f.exception()
        if exc is not None:
            logger.error(
                "db task failed: %s", exc,
                extra={"event": "db_task_fail", "task": getattr(func, "__name__", "?")},
            )

    future.add_done_callback(_done)
    return future


@contextmanager
def session_scope():
    """统一会话上下文：成功提交、异常回滚并上抛（禁止吞错）。"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def create_checkpointer():
    """在应用启动时调用，返回可长期使用的 Checkpointer（连接参数走配置）。"""
    pool = await aiomysql.create_pool(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        db=settings.MYSQL_DB,
        maxsize=10,
        autocommit=True,   # 必须！否则 setup() 建表不会提交
    )
    checkpointer = AIOMySQLSaver(pool)
    return checkpointer, pool
