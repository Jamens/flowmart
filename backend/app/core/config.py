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
