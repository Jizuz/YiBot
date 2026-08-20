from datetime import datetime
from sqlalchemy import Column, BIGINT, DateTime, Integer, String

from ..conn import Base

# 定义 user 类
class User(Base):
    __tablename__ = 'user'  # 定义表名
    id = Column(BIGINT, primary_key=True, autoincrement=True, index=True)
    name = Column(String(200), index=True, comment="用户姓名")
    full_name = Column(String(512), comment="用户全名")
    password = Column(String(200), comment="用户密码")
    hash_pwd = Column(String(200), comment="hash密码")
    mobile = Column(String(20), comment="用户手机号")
    email = Column(String(32), comment="用户邮箱")
    id_type = Column(Integer, default=0, comment="证件类型")
    id_number = Column(String(100), comment="证件号码")
    disabled = Column(Integer, comment="是否被禁用")
    gmt_create = Column(DateTime, default=datetime.now, comment="创建时间")
    gmt_update = Column(DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间")
    is_delete = Column(Integer, default=0, comment="删除标记")
    version = Column(Integer, default=0, comment="版本号")

    def __repr__(self):
        return f"<User(id={self.id}, name={self.name}, fullName={self.full_name}, hashPassword={self.hash_pwd}, mobile={self.mobile}, email={self.mobile}, disabled={self.disabled})>"

    