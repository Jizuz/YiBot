from sqlalchemy import update
from sqlalchemy.orm import Session

from core.security import hash_password, verify_password
from data.mysql.models.user import User
from schemas.user_schema import UserCreate

def create_user(db: Session, user: UserCreate):
    """创建用户
    Args:
        db:数据库实例
        user:用户模型
    Returns:
        用户信息
    """
    db_user = User(name=user.name, full_name=user.full_name, password=user.password, mobile=user.mobile, email=user.email, disabled=user.disabled)
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user

def query_user(db: Session, id: int):
    """
    根据id查询用户信息
    Args:
        db:数据库实例
        id:用户ID
    Returns:
        用户信息
    """
    return db.query(User).filter(User.id == id).first()

def query_user_by_mobile(db: Session, mobile: str):
    """
    根据id查询用户信息
    Args:
        db:数据库实例
        mobile:用户手机号
    Returns:
        用户信息
    """
    return db.query(User).filter(User.mobile == mobile).first()

def delete_by_id(db: Session, mobile: str):
    """根据用户ID删除用户信息
    Args:
        db:数据库实例
        mobile:用户手机号
    Returns:
        用户ID
    """
    exist_user = db.query(User).filter(User.mobile == mobile).first()
    if not exist_user:
        raise ValueError(f"不存在手机号{mobile}的用户")

    db.delete(exist_user)
    db.commit()

    return exist_user.id


# ---------------- 密码哈希迁移（T8，spec: security-hardening） ----------------

def create_user_hashed(db: Session, *, mobile: str, password: str, name: str | None = None, email: str | None = None) -> User:
    """新用户注册：仅写 bcrypt 哈希，明文列置空（CR 红线：明文不再落库）。"""
    db_user = User(
        name=name or f"用户{mobile[-4:]}",
        mobile=mobile,
        email=email,
        password="",
        hash_pwd=hash_password(password),
        disabled=0,
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


def upgrade_password_if_needed(db: Session, mobile: str, plain: str) -> bool:
    """存量明文用户惰性升级：条件更新（hash_pwd 为空才写），防并发双写。

    Returns:
        True=本次完成升级；False=无需升级（已升级或用户不存在）。
    """
    result = db.execute(
        update(User)
        .where(
            User.mobile == mobile,
            (User.hash_pwd.is_(None)) | (User.hash_pwd == ""),
        )
        .values(hash_pwd=hash_password(plain), password="")
    )
    db.commit()
    return result.rowcount == 1


def authenticate(db: Session, mobile: str, password: str) -> User | None:
    """登录校验（双读）：哈希优先，存量明文匹配成功则惰性升级。

    Returns:
        校验通过返回 User；账号不存在/密码错误/已禁用一律返回 None
        （上层统一返回 BAD_CREDENTIALS，不区分存在性——防撞库探测）。
    """
    user = (
        db.query(User)
        .filter(User.mobile == mobile, User.disabled == 0)
        .first()
    )
    if user is None:
        return None
    if user.hash_pwd:
        return user if verify_password(password, user.hash_pwd) else None
    if user.password and user.password == password:  # 存量明文双读
        upgrade_password_if_needed(db, mobile, password)
        db.refresh(user)
        return user
    return None