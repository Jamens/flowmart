"""全局配置：所有可变参数集中在此，支持 .env 覆盖。"""
from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> parents[3] 即项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """应用配置。默认值可直接跑，生产环境用 .env 覆盖。"""

    PROJECT_NAME: str = "flowmart"
    API_V1_PREFIX: str = "/api/v1"
    DEBUG: bool = True

    # JWT 签名密钥：生产必须改成强随机值并通过环境变量注入，默认仅开发可用
    SECRET_KEY: str = "dev-only-insecure-secret-change-me"
    # 短期访问令牌：过期靠 /auth/refresh 静默续期，故设短（默认 30 分钟）。生产可调。
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # 初始管理员用户名：首次 seed / 迁移（init_db、seed）时把该用户提升为管理员。
    # 生产务必改为真实管理员账号，切勿沿用演示值 zhangsan。
    BOOTSTRAP_ADMIN: str = "zhangsan"

    # 跨域（CORS）：仅放行已知前端源，禁止随意 `*`（配合 credentials 使用时 `*` 会被浏览器拒绝）
    CORS_ORIGINS: list[str] = ["http://127.0.0.1:5173", "http://localhost:5173"]

    # JWT 写入 httpOnly Cookie（防御 XSS 窃令牌，前端不再用 localStorage 存明文令牌）
    JWT_COOKIE_NAME: str = "fm_token"
    COOKIE_SECURE: bool = False  # 生产必须 True（仅 HTTPS 下浏览器才接受 httpOnly+Secure）
    COOKIE_SAMESITE: str = "lax"  # lax / strict：lax 兼容同站前端，strict 抗 CSRF 更强

    # 刷新令牌：长期有效、写独立 httpOnly Cookie，仅用于 /auth/refresh 换发访问令牌
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    REFRESH_TOKEN_COOKIE_NAME: str = "fm_refresh"

    # 登录限流（防暴力破解）：同一 (客户端IP, 用户名) 在窗口内失败超阈值即拒。
    # 内存级固定窗口——单实例足够、零依赖；多实例/生产需换 Redis 等共享存储，
    # 否则限流只对本机请求生效（注释见 backend/app/core/ratelimit.py）。
    LOGIN_RATE_LIMIT_MAX: int = 5
    LOGIN_RATE_LIMIT_WINDOW: int = 60  # 秒

    # 登录限流——客户端 IP 来源（安全开关，默认 False）：
    # False（默认）→ 取 request.client.host（直连真实 socket 地址，客户端无法伪造）；
    # True  → 仅当反向代理已用真实客户端 IP 覆写 X-Forwarded-For 时才取其首跳。
    # 切勿在「客户端可自己塞 X-Forwarded-For」的直连场景开 True，否则攻击者可伪造
    # 不同 XFF 让每次请求都生成新限流键、永远累计不到阈值，限流直接失效。
    LOGIN_RATE_LIMIT_TRUST_PROXY: bool = False

    # 登录限流——多实例/负载均衡共享计数（留空=进程内内存，单实例够用）：
    # 非空时启用 Redis 后端（INCR+EXPIRE 原子计数），限流对全部实例统一生效。
    # 例如 "redis://127.0.0.1:6379/0"。连不上会在启动时直接报错（fail-fast）。
    LOGIN_RATE_LIMIT_REDIS_URL: str = ""

    # ---- 通用限流（注册 / 验证码 / 找回密码）：防灌账号、验证码轰炸 ----
    # 与登录限流的区别：登录只记**失败**（防密码爆破），这里记**每次请求**——
    # 这类端点不看成败只看调用量，一次成功调用同样消耗配额，
    # 否则攻击者可用正确参数高频调用把短信/邮件渠道打爆、或刷出一堆账号。
    # 计数后端与登录限流共用（同一个 LOGIN_RATE_LIMIT_REDIS_URL）。
    RATE_LIMIT_REGISTER_MAX: int = 5  # 每 IP 每窗口最多注册次数
    RATE_LIMIT_CODE_MAX: int = 5  # 每 IP 每窗口最多发码次数（验证 / 找回密码）
    # 登录的 **per-IP** 上限，与上面「登录失败限流」互补：
    # 后者按 (IP, 用户名) 只记失败，换用户名就能重置配额；而每次密码校验都要跑
    # PBKDF2（约几十毫秒 CPU），于是「用户名 × 5 次」会变成 CPU 放大 DoS
    # 与凭证填充的通道。这一层不看用户名也不看成败，只看这个 IP 的登录请求量。
    RATE_LIMIT_LOGIN_MAX: int = 30
    # OTP 确认（6 位码）的猜测上限，按 (IP, 用户名) 记**每次请求**：
    # 每条码自身的 OTP_CONFIRM_MAX_ATTEMPTS 会被「重新发码」重置，
    # 稳态猜码速率 = 尝试数/码 × 码数/分，不额外限住就是个可用的爆破通道。
    RATE_LIMIT_OTP_MAX: int = 10
    # 下单 / 结算：按 **user_id**（不是 IP）计每次请求。create_order 会原子扣库存，
    # 刷单能把库存打到 0 —— 业务型 DoS，比打爆 CPU 更难恢复。
    RATE_LIMIT_ORDER_MAX: int = 20
    RATE_LIMIT_WINDOW: int = 60  # 秒

    # ---- 图片上传（商品封面等）----
    # 本地磁盘 + 静态目录挂载，零外部依赖；换 OSS/S3 只需替换 api/uploads.py
    # 的写入逻辑，对外返回的 URL 形状不变。
    UPLOAD_DIR: str = str(PROJECT_ROOT / "uploads")
    UPLOAD_URL_PREFIX: str = "/uploads"
    UPLOAD_MAX_BYTES: int = 2 * 1024 * 1024  # 单文件上限 2MB
    # 允许的图片类型按**文件头魔数**判定而非信任 Content-Type（见 api/uploads.py），
    # 故这里不提供「允许的类型」配置——类型白名单与魔数表是一一对应的，
    # 放开配置项反而容易配出「声称 png 实际放行任意内容」的洞。

    # 邮箱/手机验证码（OTP）：申请→确认两步式，确认后标记对应渠道已验证。
    OTP_LENGTH: int = 6  # 验证码位数（纯数字）
    OTP_TTL_SECONDS: int = 600  # 验证码有效期（10 分钟）
    # 重发限流（基于 DB，天然多实例安全）：同一用户对同一渠道在窗口内最多发 N 次
    OTP_RESEND_WINDOW: int = 60  # 秒
    OTP_MAX_PER_WINDOW: int = 5
    # 单个验证码的确认尝试上限：超过即锁定该码（防对 6 位码暴力枚举）
    OTP_CONFIRM_MAX_ATTEMPTS: int = 5
    # 发送接口是否把验证码直接回传（仅开发便利）。生产必须 False，否则等于把验证码明文交给客户端。
    OTP_DEV_RETURN_CODE: bool = True
    # 验证码发送器：当前仅 "console"（print 到日志，开发可用）；smtp / sms 为可插拔扩展点，未实现时配了会启动报错。
    OTP_SENDER: str = "console"

    # MySQL 连接
    DB_HOST: str = "127.0.0.1"
    DB_PORT: int = 3306
    DB_USER: str = "root"
    DB_PASSWORD: str = "1234560"
    DB_NAME: str = "flowmart"
    DB_ECHO: bool = False

    # 数据库类型：sqlite（默认，开发期单文件便于查看）或 mysql（生产）
    DB_DIALECT: str = "sqlite"
    # SQLite 文件路径，留空则默认落在项目根目录 flowmart.db
    SQLITE_PATH: str = ""

    # 直接给定完整连接串时优先级最高，用于连特殊环境
    DATABASE_URL: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def _require_strong_secret_in_prod(self):
        # 生产（非 debug）下仍用开发默认密钥意味着任何人都能伪造令牌，必须启动即报错
        if not self.DEBUG and self.SECRET_KEY == "dev-only-insecure-secret-change-me":
            raise ValueError("生产环境必须设置强随机 SECRET_KEY，当前仍为开发默认值")
        return self

    @model_validator(mode="after")
    def _require_secure_cookie_in_prod(self):
        # 生产（非 debug）下若 COOKIE_SECURE 仍为 False，HttpOnly Cookie 会经明文 HTTP 传输，
        # 网络中间人可直接窃取会话令牌。必须启动即报错，而不是悄悄裸奔。
        if not self.DEBUG and not self.COOKIE_SECURE:
            raise ValueError("生产环境必须 COOKIE_SECURE=True，否则会话 Cookie 会经明文 HTTP 泄露")
        return self

    @model_validator(mode="after")
    def _require_bootstrap_admin(self):
        # 去掉首尾空白后再判断：裸 " zhangsan " 这类带空格的值若不归一化，
        # 会和真实用户名匹配不上，导致「谁都不是管理员」、系统静默锁死。
        # 配置错误必须在启动时暴露，而不是运行时悄悄变成不可用。
        self.BOOTSTRAP_ADMIN = self.BOOTSTRAP_ADMIN.strip()
        if not self.BOOTSTRAP_ADMIN:
            raise ValueError("BOOTSTRAP_ADMIN 不能为空：必须指定一个初始管理员用户名")
        return self

    @model_validator(mode="after")
    def _require_no_dev_otp_in_prod(self):
        # 生产（非 debug）下若 OTP_DEV_RETURN_CODE 仍为 True，发送验证码接口会把明文 OTP 回传给客户端，
        # 等于没有验证。必须启动即报错，绝不悄悄把验证码交给前端。
        if not self.DEBUG and self.OTP_DEV_RETURN_CODE:
            raise ValueError("生产环境必须 OTP_DEV_RETURN_CODE=False，否则验证码会被明文回传")
        return self

    @property
    def database_url(self) -> str:
        """最终连接串，优先级：DATABASE_URL > DB_DIALECT > MySQL 参数拼装。"""
        if self.DATABASE_URL:
            return self.DATABASE_URL
        if self.DB_DIALECT == "sqlite":
            path = Path(self.SQLITE_PATH) if self.SQLITE_PATH else PROJECT_ROOT / "flowmart.db"
            return f"sqlite:///{path.as_posix()}"
        # charset=utf8mb4 保证 emoji 与生僻字不炸
        return (
            f"mysql+pymysql://{self.DB_USER}:{quote(self.DB_PASSWORD)}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}?charset=utf8mb4"
        )

    @property
    def server_database_url(self) -> str:
        """不带库名的连接串，用于建库（仅 MySQL 需要）。"""
        return (
            f"mysql+pymysql://{self.DB_USER}:{quote(self.DB_PASSWORD)}"
            f"@{self.DB_HOST}:{self.DB_PORT}/?charset=utf8mb4"
        )

    @property
    def dialect(self) -> str:
        """当前数据库方言名，用于区分 MySQL / SQLite 的差异化处理。"""
        return self.database_url.split("://")[0].split("+")[0]


def quote(pwd: str) -> str:
    """密码里的 @ / : / / 等字符必须转义，否则连接串会被截断解析。"""
    from urllib.parse import quote_plus

    return quote_plus(pwd)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
