from datetime import datetime
from sqlalchemy import BigInteger, Column, DateTime, Integer, String, Text

from ..conn import Base


class Session(Base):
    __tablename__ = "session"
    id = Column("id", BigInteger, primary_key=True, autoincrement=True)
    session_id = Column("session_id", String(64), nullable=False, unique=True)
    user_id = Column("user_id", String(64), nullable=False)
    status = Column("status", String(16), default="active", nullable=False)
    create_time = Column("create_time", DateTime, nullable=False, default=datetime.now)
    last_active_time = Column("last_active_time", DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)
    close_time = Column("close_time", DateTime, nullable=True)

class SessionMessage(Base):
    __tablename__ = "session_message"
    id = Column("id", BigInteger, primary_key=True, autoincrement=True)
    session_id = Column("session_id", String(64), nullable=False)
    type = Column("type", Integer)
    content = Column("content", Text)
    create_time = Column("create_time", DateTime, nullable=False, default=datetime.now)
