"""集中配置（T4，spec: production-hardening）。

- pydantic-settings 单一配置源：字段名与既有环境变量同名，天然向后兼容；
- 各层读取点在 P4 归位时统一切换至 get_settings()，此前 os.getenv 路径并存；
- 密钥类字段无默认值注入代码（空串占位，使用点负责非空校验——CR 安全红线）。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- LLM（GLM）----
    GLM_API_KEY: str = ""
    GLM_MODEL_ID: str = ""
    GLM_BASE_URL: str = ""
    LLM_TEMPERATURE: float = 0.7
    LLM_TIMEOUT_SECONDS: float = 60.0
    LLM_MAX_RETRIES: int = 2

    # ---- 鉴权（P1：使用点校验非空）----
    JWT_SECRET: str = ""
    JWT_REFRESH_SECRET: str = ""
    JWT_ACCESS_TTL_MIN: int = 30
    JWT_REFRESH_TTL_DAYS: int = 7
    AUTH_ENABLED: bool = True

    # ---- LLM 配额（P2）----
    LLM_DAILY_TOKEN_QUOTA: int = 50000
    QUOTA_ENFORCE: bool = True
    QUOTA_WARN_RATIO: float = 0.8

    # ---- 高德 ----
    AMAP_KEY: str = ""

    # ---- MySQL ----
    DATABASE_URI: str = ""
    MYSQL_HOST: str = "127.0.0.1"
    MYSQL_PORT: int = 3306
    MYSQL_USER: str = ""
    MYSQL_PASSWORD: str = ""
    MYSQL_DB: str = ""

    # ---- Redis ----
    REDIS_HOST: str = "127.0.0.1"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: str = ""

    # ---- 搜索工具 ----
    TAVILY_API_KEY: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
