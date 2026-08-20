from sqlalchemy.orm import Session

from database.models.user import User
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
        print(f">>>>>> 不存在手机号{mobile}的用户")
        raise ValueError
    
    db.delete(exist_user)
    db.commit()

    return exist_user.id