from datetime import datetime

from pydantic import BaseModel

class UserBase(BaseModel):
    name: str | None = None
    full_name: str | None = None
    mobile: str | None = None
    email: str | None = None
    id_type: int | None = None
    id_number: str | None = None
    disabled: int | None = 0
    gmt_create: datetime | None = None
    gmt_update: datetime | None = None
    is_delete : int | None = 0
    version: int | None = 0

class UserCreate(UserBase):
    password: str | None