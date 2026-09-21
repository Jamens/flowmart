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
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 默认 1 天

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
