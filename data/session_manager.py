"""会话管理（T20 异步化，spec: production-hardening）。

- 会话态：redis.asyncio（经 data.redis_client 单例），全方法协程化——
  消除同步客户端阻塞事件循环（design: 异步与资源红线）；
- 持久化：MySQL 经 submit_db_task 受控后台任务（无上限裸线程已废除）；
- Redis 键格式与存量完全一致（agent:session/context/user:*，900s TTL）。
"""

import logging
from datetime import datetime
from typing import List, Optional
import uuid

from data.mysql import session_repository
from data.mysql.conn import submit_db_task
from data.redis_client import get_redis

logger = logging.getLogger("yibot.data.session")

SESSION_EXPIRE_SECONDS = 900  # 15分钟会话超时


class SessionManager:
    repo = session_repository.SessionRepository

    @staticmethod
    def generate_session_id() -> str:
        return str(uuid.uuid4())

    @staticmethod
    async def get_user_active_session(user_id: str) -> Optional[str]:
        redis = get_redis()
        return await redis.get(f"agent:user:{user_id}:session")

    @staticmethod
    def create_session_mysql(session_id: str, user_id: str):
        submit_db_task(SessionManager.repo.create_session, session_id, user_id)

    @staticmethod
    def save_message_mysql(session_id: str, type: int, msg: str):
        submit_db_task(SessionManager.repo.add_message, session_id, type, msg)

    @staticmethod
    def update_session_status_mysql(session_id: str, status: str):
        logger.info(
            "session close in db",
            extra={"event": "session_close_db", "session_id": session_id, "status": status},
        )
        submit_db_task(SessionManager.repo.update_session_status, session_id, status)

    @staticmethod
    async def create_new_session(user_id: str) -> str:
        redis = get_redis()
        session_id = SessionManager.generate_session_id()

        # Redis初始化会话
        session_hash_key = f"agent:session:{session_id}"
        session_data = {
            "session_id": session_id,
            "user_id": user_id,
            "status": "active",
        }
        await redis.hset(session_hash_key, mapping=session_data)
        await redis.expire(session_hash_key, SESSION_EXPIRE_SECONDS)

        user_key = f"agent:user:{user_id}:session"
        await redis.setex(user_key, SESSION_EXPIRE_SECONDS, session_id)

        context_key = f"agent:context:{session_id}"
        await redis.expire(context_key, SESSION_EXPIRE_SECONDS)

        # 受控后台落库
        SessionManager.create_session_mysql(session_id, user_id)
        return session_id

    @staticmethod
    async def refresh_session(session_id: str, user_id: str):
        redis = get_redis()
        session_hash_key = f"agent:session:{session_id}"
        context_key = f"agent:context:{session_id}"
        user_key = f"agent:user:{user_id}:session"
        now_ts = datetime.now().time()

        await redis.hset(session_hash_key, "last_active", str(now_ts))
        await redis.expire(session_hash_key, SESSION_EXPIRE_SECONDS)
        await redis.expire(context_key, SESSION_EXPIRE_SECONDS)
        await redis.expire(user_key, SESSION_EXPIRE_SECONDS)

    @staticmethod
    async def get_or_create_session(user_id: str) -> str:
        active_session = await SessionManager.get_user_active_session(user_id)
        if active_session:
            await SessionManager.refresh_session(active_session, user_id)
            return active_session
        return await SessionManager.create_new_session(user_id)

    @staticmethod
    async def append_chat_message(session_id: str, type: int, msg: str):
        redis = get_redis()
        context_key = f"agent:context:{session_id}"
        item = f"TYPE:{type}|AGENT:{msg}"
        await redis.rpush(context_key, item)
        # 受控异步持久化
        SessionManager.save_message_mysql(session_id, type, msg)

    @staticmethod
    async def get_context(session_id: str) -> List[str]:
        redis = get_redis()
        return await redis.lrange(f"agent:context:{session_id}", 0, -1)

    @staticmethod
    async def close_session(session_id: str, user_id: str):
        redis = get_redis()
        # 清空缓存
        await redis.delete(f"agent:session:{session_id}")
        await redis.delete(f"agent:context:{session_id}")
        await redis.delete(f"agent:user:{user_id}:session")
        # 更新数据库状态
        SessionManager.update_session_status_mysql(session_id, "closed")

    @staticmethod
    async def freeze_session(session_id: str):
        SessionManager.update_session_status_mysql(session_id, "frozen")
