from datetime import datetime
from typing import List, Optional
import uuid
import redis

from database import session_repository
from database.conn import async_db_save

SESSION_EXPIRE_SECONDS = 900  # 15分钟会话超时

# Redis配置
REDIS_CLIENT = redis.Redis(
    host="127.0.0.1",
    port=6379,
    db=0,
    decode_responses=True,
    retry_on_timeout=True
)

class SessionManager:
    repo = session_repository.SessionRepository

    @staticmethod
    def generate_session_id() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def get_user_active_session(user_id: str) -> Optional[str]:
        key = f"agent:user:{user_id}:session"
        return REDIS_CLIENT.get(key)

    @staticmethod
    @async_db_save
    def create_session_mysql(session_id: str, user_id: str):
        SessionManager.repo.create_session(session_id, user_id)

    @staticmethod
    @async_db_save
    def save_message_mysql(session_id: str, type: int, msg: str):
        SessionManager.repo.add_message(session_id, type, msg)

    @staticmethod
    @async_db_save
    def update_session_status_mysql(session_id: str, status: str):
        print(f"handle session close, session_id: {session_id}, status: {status}")
        SessionManager.repo.update_session_status(session_id, status)

    @staticmethod
    def create_new_session(user_id: str) -> str:
        session_id = SessionManager.generate_session_id()

        # Redis初始化会话
        session_hash_key = f"agent:session:{session_id}"
        session_data = {
            "session_id": session_id,
            "user_id": user_id,
            "status": "active"
        }
        REDIS_CLIENT.hset(session_hash_key, mapping=session_data)
        REDIS_CLIENT.expire(session_hash_key, SESSION_EXPIRE_SECONDS)

        user_key = f"agent:user:{user_id}:session"
        REDIS_CLIENT.setex(user_key, SESSION_EXPIRE_SECONDS, session_id)

        context_key = f"agent:context:{session_id}"
        REDIS_CLIENT.expire(context_key, SESSION_EXPIRE_SECONDS)

        # 调用Repository落库
        SessionManager.create_session_mysql(session_id, user_id)
        return session_id

    @staticmethod
    def refresh_session(session_id: str, user_id: str):
        session_hash_key = f"agent:session:{session_id}"
        context_key = f"agent:context:{session_id}"
        user_key = f"agent:user:{user_id}:session"
        now_ts = datetime.now().time()

        REDIS_CLIENT.hset(session_hash_key, "last_active", str(now_ts))
        REDIS_CLIENT.expire(session_hash_key, SESSION_EXPIRE_SECONDS)
        REDIS_CLIENT.expire(context_key, SESSION_EXPIRE_SECONDS)
        REDIS_CLIENT.expire(user_key, SESSION_EXPIRE_SECONDS)

    @staticmethod
    def get_or_create_session(user_id: str) -> str:
        active_session = SessionManager.get_user_active_session(user_id)
        if active_session:
            SessionManager.refresh_session(active_session, user_id)
            return active_session
        return SessionManager.create_new_session(user_id)

    @staticmethod
    def append_chat_message(session_id: str, type: int, msg: str):
        # 缓存写入
        context_key = f"agent:context:{session_id}"
        item = f"TYPE:{type}|AGENT:{msg}"
        REDIS_CLIENT.rpush(context_key, item)
        # 异步持久化
        SessionManager.save_message_mysql(session_id, type, msg)

    @staticmethod
    def get_context(session_id: str) -> List[str]:
        context_key = f"agent:context:{session_id}"
        return REDIS_CLIENT.lrange(context_key, 0, -1)

    @staticmethod
    def close_session(session_id: str, user_id: str):
        # 清空缓存
        REDIS_CLIENT.delete(f"agent:session:{session_id}")
        REDIS_CLIENT.delete(f"agent:context:{session_id}")
        REDIS_CLIENT.delete(f"agent:user:{user_id}:session")
        # 更新数据库状态
        SessionManager.update_session_status_mysql(session_id, "closed")

    @staticmethod
    def freeze_session(session_id: str):
        SessionManager.update_session_status_mysql(session_id, "frozen")

    def get_user_session_list(user_id: str, page: int = 1, page_size: int = 10):
        """
        分页获取用户所有历史会话列表
        按最后活跃时间倒序，最新会话在最前
        """
        offset = (page - 1) * page_size
        # 查询总数量
        total = SessionManager.repo.count_session_list(user_id)
        # 分页查询会话列表
        session_list = SessionManager.repo.get_session_list(user_id, offset, page_size)

        records = []
        for item in session_list:
            message = SessionManager.repo.get_user_session_first_message(item.session_id)
            records.append({
                "sessionId": item.session_id,
                "status": item.status,
                "createTime": str(item.create_time),
                "lastActiveTime": str(item.last_active_time),
                "closeTime": str(item.close_time) if item.close_time else None,
                "abstract": message if message else "---"
            })

        return {
            "total": total,
            "page": page,
            "pageSize": page_size,
            "records": records
        }

    def get_session_playback_data(session_id: str):
        """获取会话详情+消息列表（前端历史展示专用）"""
        info = SessionManager.repo.get_session_info(session_id)
        msg_list = SessionManager.repo.get_session_message_list(session_id)
        if not info:
            return None
        return {
            "sessionInfo": {
                "sessionId": info.session_id,
                "status": info.status,
                "createTime": str(info.create_time)
            },
            "messageList": [
                {
                    "userContent": m.content if m.type == 0 else None,
                    "agentContent": m.content if m.type == 1 else None,
                    "createTime": str(m.create_time)
                }
                for m in msg_list
            ]
        }
