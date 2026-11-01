"""鉴权相关 API DTO。"""

from pydantic import BaseModel, field_validator


class LoginIn(BaseModel):
    mobile: str
    password: str

    @field_validator("mobile")
    @classmethod
    def validate_mobile(cls, value: str) -> str:
        value = value.strip()
        if not (len(value) == 11 and value.startswith("1") and value.isdigit()):
            raise ValueError("手机号格式不正确")
        return value

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if len(value) < 4:
            raise ValueError("密码至少 4 位")
        return value


class RefreshIn(BaseModel):
    refresh_token: str


class LogoutIn(BaseModel):
    refresh_token: str | None = None


class UserInfoOut(BaseModel):
    uid: int | str
    name: str | None = None
    mobile: str | None = None
    role: str = "user"


class TokenPairOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_granted: bool = True
    user: UserInfoOut
