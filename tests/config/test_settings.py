"""T4：配置集中管理测试（spec: production-hardening / design: 方案选型#10）。

红阶段断言口径：
- 默认值正确（鉴权/配额开关、TTL、配额额度、模型参数）；
- 环境变量覆盖生效（旧变量名天然兼容：字段名与现有 env 同名）；
- get_settings() 单例；
- env_file 可禁用（保证测试不受本地 .env 干扰）。
"""

from config.settings import Settings, get_settings


def _fresh(monkeypatch, **env_items):
    """构造一个不读 .env、只受 os.environ 影响的 Settings。"""
    for key in (
        "JWT_SECRET", "JWT_REFRESH_SECRET", "AMAP_KEY", "GLM_MODEL_ID",
        "LLM_DAILY_TOKEN_QUOTA", "REDIS_HOST",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env_items.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def test_defaults(monkeypatch):
    settings = _fresh(monkeypatch)
    assert settings.AUTH_ENABLED is True
    assert settings.QUOTA_ENFORCE is True
    assert settings.JWT_ACCESS_TTL_MIN == 30
    assert settings.JWT_REFRESH_TTL_DAYS == 7
    assert settings.LLM_DAILY_TOKEN_QUOTA == 50000
    assert settings.QUOTA_WARN_RATIO == 0.8
    assert settings.LLM_TEMPERATURE == 0.7
    assert settings.LLM_TIMEOUT_SECONDS > 0
    assert settings.LLM_MAX_RETRIES >= 0


def test_env_override(monkeypatch):
    settings = _fresh(
        monkeypatch,
        JWT_SECRET="s1",
        JWT_REFRESH_SECRET="s2",
        AMAP_KEY="amap-new",
        GLM_MODEL_ID="glm-4",
        LLM_DAILY_TOKEN_QUOTA="100",
        REDIS_HOST="10.0.0.2",
    )
    assert settings.JWT_SECRET == "s1"
    assert settings.JWT_REFRESH_SECRET == "s2"
    assert settings.AMAP_KEY == "amap-new"
    assert settings.GLM_MODEL_ID == "glm-4"
    assert settings.LLM_DAILY_TOKEN_QUOTA == 100
    assert settings.REDIS_HOST == "10.0.0.2"


def test_bool_override(monkeypatch):
    settings = _fresh(monkeypatch, AUTH_ENABLED="false", QUOTA_ENFORCE="false")
    assert settings.AUTH_ENABLED is False
    assert settings.QUOTA_ENFORCE is False


def test_get_settings_is_singleton():
    first = get_settings()
    second = get_settings()
    assert first is second
    get_settings.cache_clear()
