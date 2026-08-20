from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import user_repository
from database.conn import SessionLocal, Base, engine
from schemas import user_schema

router = APIRouter(prefix="/user", tags=["用户管理"])

def get_db():
    try:
        db = SessionLocal()
        yield db
    finally:
        db.close()

@router.post("/add", response_model=user_schema.UserBase)
def add_user(user: user_schema.UserCreate, db: Session = Depends(get_db)):
    if user.mobile:
        exist_user = user_repository.query_user_by_mobile(db, user.mobile)
        if exist_user:
            raise HTTPException(status_code=400, detail="手机号已经被注册")

    db_user = user_repository.create_user(db, user)
    print(f"user add_user success, db_user: {db_user.__repr__}")
    return db_user

@router.get("/detail/mobile")
def query_by_mobile(mobile: str, db: Session = Depends(get_db)):
    if not mobile:
        raise HTTPException(status_code=400, detail="查询异常")
    
    return user_repository.query_user_by_mobile(db, mobile)

@router.post("/delete/{mobile}")
def delete_by_mobile(mobile: str, db: Session = Depends(get_db)):
    if not mobile:
        raise HTTPException(status_code=400, detail="手机号异常")

    try:
        return user_repository.delete_by_id(db, mobile)
    except Exception:
        raise HTTPException(status_code=500, detail="删除失败")
