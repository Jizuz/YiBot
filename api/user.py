from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from data.mysql import user_repository
from data.mysql.conn import SessionLocal, Base, engine
from schemas import user_schema

router = APIRouter(prefix="/user", tags=["用户管理"])

# 过渡期脱敏：旧伪登录接口响应禁止携带任何密码字段（spec: access-auth）
_SAFE_FIELDS = ("id", "name", "full_name", "mobile", "email", "disabled",
                "gmt_create", "gmt_update", "is_delete", "version")


def _sanitize(user) -> dict:
    return {key: getattr(user, key, None) for key in _SAFE_FIELDS}

def get_db():
    try:
        db = SessionLocal()
        yield db
    finally:
        db.close()

@router.post("/add")
def add_user(user: user_schema.UserCreate, db: Session = Depends(get_db)):
    if not user.mobile:
        raise HTTPException(status_code=400, detail="手机号输入异常")

    # 校验手机号注册
    exist_user = user_repository.query_user_by_mobile(db, user.mobile)
    if exist_user:
        raise HTTPException(status_code=400, detail="手机号已经被注册")

    db_user = user_repository.create_user(db, user)
    db_user = user_repository.query_user_by_mobile(db, user.mobile)
    return _sanitize(db_user)

@router.get("/detail/mobile")
def query_by_mobile(mobile: str, db: Session = Depends(get_db)):
    if not mobile:
        raise HTTPException(status_code=400, detail="查询异常")

    user = user_repository.query_user_by_mobile(db, mobile)
    if user is None:
        return None
    return _sanitize(user)

@router.post("/delete/{mobile}")
def delete_by_mobile(mobile: str, db: Session = Depends(get_db)):
    if not mobile:
        raise HTTPException(status_code=400, detail="手机号异常")

    try:
        return user_repository.delete_by_id(db, mobile)
    except ValueError:
        raise HTTPException(status_code=500, detail="删除失败")
