"""统一业务码段（T21 下沉至 core：横切层不得反向依赖接入层）。

- 0 成功 / 40101~40199 鉴权 / 42900~42999 配额 / 50000 系统异常；
- api/responses 与 core/security 共用本定义（单一事实源）。
"""


class BIZ_CODE:
    OK = 0
    # 鉴权段 40100-40199
    UNAUTHORIZED = 40101      # 未登录
    TOKEN_EXPIRED = 40102     # 令牌过期
    TOKEN_INVALID = 40103     # 令牌无效（含篡改/类型错误/已轮换）
    TOKEN_REVOKED = 40104     # 已吊销
    BAD_CREDENTIALS = 40105   # 手机号或密码错误
    # 配额段 42900-42999
    QUOTA_EXCEEDED = 42901    # 当日额度用尽
    # 系统段
    INTERNAL_ERROR = 50000
