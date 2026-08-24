from datetime import datetime

from database.conn import SessionLocal
from database.models.session import Session, SessionMessage

# 数据库会话工具
def get_db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class SessionRepository:
    """会话数据仓库：仅负责数据库CRUD"""

    @staticmethod
    def create_session(session_id: str, user_id: str):
        """创建会话主记录"""
        db = next(get_db_session())
        try:
            obj = Session(
                session_id=session_id,
                user_id=user_id,
                status="active"
            )
            db.add(obj)
            db.commit()
        except Exception as e:
            db.rollback()
            raise e
        finally:
            db.close()

    @staticmethod
    def add_message(session_id: str, type: int, msg: str):
        """新增单条对话消息"""
        db = next(get_db_session())
        try:
            obj = SessionMessage(
                session_id=session_id,
                type=type,
                content=msg
            )
            db.add(obj)
            db.commit()
        except Exception as e:
            db.rollback()
            raise e
        finally:
            db.close()

    @staticmethod
    def update_session_status(session_id: str, status: str):
        """更新会话状态：active/frozen/closed"""
        print(f"session status update, session_id: {session_id}, status: {status}")
        db = next(get_db_session())
        try:
            session_obj = db.query(Session).filter(Session.session_id == session_id).first()
            if not session_obj:
                return
            session_obj.status = status
            if status == "closed":
                session_obj.close_time = datetime.now()
            # TODO
            db.commit()
        except Exception as e:
            db.rollback()
            raise e
        finally:
            db.close()

    @staticmethod
    def batch_freeze_timeout_session(expire_time: datetime):
        """批量冻结超时会话（定时任务）"""
        db = next(get_db_session())
        try:
            rows = db.query(Session)\
                .filter(Session.status == "active")\
                .filter(Session.last_active_time <= expire_time)\
                .update({"status": "frozen"})
            db.commit()
            return rows
        except Exception as e:
            db.rollback()
            raise e
        finally:
            db.close()

    @staticmethod
    def count_session_list(user_id: str):
        db = next(get_db_session())
        # 查询总数量
        total = db.query(Session).filter(Session.user_id == user_id).count()
        return total

    @staticmethod
    def get_session_list(user_id: str, offset: int, page_size: int):
        """分页查询用户会话列表"""
        db = next(get_db_session())
        try:
            # 分页查询会话列表
            session_list = db.query(Session)\
                .filter(Session.user_id == user_id)\
                .order_by(Session.last_active_time.desc())\
                .offset(offset)\
                .limit(page_size)\
                .all()
            return session_list
        except Exception as e:
            return []
        finally:
            db.close()

    @staticmethod
    def get_user_session_first_message(session_Id: str):
        """查询用户某个会话等第一条信息"""
        db = next(get_db_session())
        try:
            message = db.query(SessionMessage)\
                        .filter(SessionMessage.session_id == session_Id)\
                        .order_by(SessionMessage.create_time.asc())\
                        .first()
            return message.content if message else None
        except Exception as e:
            return None
        finally:
            db.close()

    @staticmethod
    def get_session_message_list(session_id: str):
        """根据会话ID查询所有历史消息（前端回放/历史记录）"""
        db = next(get_db_session())
        try:
            return db.query(SessionMessage)\
                .filter(SessionMessage.session_id == session_id)\
                .order_by(SessionMessage.create_time.asc())\
                .all()
        finally:
            db.close()

    @staticmethod
    def get_session_info(session_id: str):
        """查询会话基础信息"""
        db = next(get_db_session())
        try:
            return db.query(Session).filter(Session.session_id == session_id).first()
        finally:
            db.close()
