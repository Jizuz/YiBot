"""T8：密码哈希迁移测试（spec: security-hardening / design: 存储设计）。

场景：新注册只写哈希、存量明文登录惰性升级（条件更新防并发双写）、
升级后走哈希校验、幂等、错误密码不迁移、禁用账号拒绝。
"""

import pytest
from sqlalchemy import BigInteger, Integer, create_engine
from sqlalchemy.orm import sessionmaker

from core.security import hash_password, verify_password
from data.mysql import user_repository as repo
from data.mysql.conn import Base
from data.mysql.models.user import User


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    # sqlite 仅支持 INTEGER PRIMARY KEY 自增：为测试方言替换列类型（不影响 MySQL 生产 DDL）
    User.__table__.c.id.type = BigInteger().with_variant(Integer(), "sqlite")
    Base.metadata.create_all(engine, tables=[User.__table__])
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _seed_plain_user(db, mobile="13800000001", password="plain-pw"):
    db.add(User(name="存量用户", mobile=mobile, password=password, disabled=0))
    db.commit()


# ---------------- 新注册 ----------------

def test_create_user_hashed_only(db):
    user = repo.create_user_hashed(db, mobile="13900000002", password="NewPw123")
    assert user.hash_pwd and user.hash_pwd.startswith("$2")
    assert not (user.password or "")  # 明文列置空
    assert verify_password("NewPw123", user.hash_pwd)


# ---------------- 存量明文惰性升级 ----------------

def test_plain_user_authenticates_and_upgrades(db):
    _seed_plain_user(db)
    user = repo.authenticate(db, "13800000001", "plain-pw")
    assert user is not None
    db.expire_all()
    fresh = repo.query_user_by_mobile(db, "13800000001")
    assert fresh.hash_pwd and fresh.hash_pwd.startswith("$2")
    assert not (fresh.password or "")
    assert verify_password("plain-pw", fresh.hash_pwd)


def test_upgraded_user_logs_in_via_hash(db):
    _seed_plain_user(db)
    assert repo.authenticate(db, "13800000001", "plain-pw") is not None
    # 明文已清空，二次登录必须走哈希且成功
    assert repo.authenticate(db, "13800000001", "plain-pw") is not None


def test_upgrade_is_idempotent_and_conditional(db):
    _seed_plain_user(db)
    assert repo.upgrade_password_if_needed(db, "13800000001", "plain-pw") is True
    # 已升级后再次调用：条件（hash_pwd 为空）不满足 → False（防并发双写）
    assert repo.upgrade_password_if_needed(db, "13800000001", "plain-pw") is False


def test_wrong_password_no_migration(db):
    _seed_plain_user(db)
    assert repo.authenticate(db, "13800000001", "wrong-pw") is None
    fresh = repo.query_user_by_mobile(db, "13800000001")
    assert fresh.password == "plain-pw"  # 未迁移
    assert not (fresh.hash_pwd or "")


def test_hashed_user_wrong_password_rejected(db):
    repo.create_user_hashed(db, mobile="13900000003", password="Right123")
    assert repo.authenticate(db, "13900000003", "Right123") is not None
    assert repo.authenticate(db, "13900000003", "Wrong123") is None


def test_disabled_user_rejected(db):
    db.add(User(name="禁用", mobile="13700000004", password="", hash_pwd=hash_password("Pw12345"), disabled=1))
    db.commit()
    assert repo.authenticate(db, "13700000004", "Pw12345") is None


def test_unknown_mobile_returns_none(db):
    assert repo.authenticate(db, "13000000000", "any") is None
