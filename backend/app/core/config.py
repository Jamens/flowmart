"""全局配置：所有可变参数集中在此，支持 .env 覆盖。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置。默认值可直接跑，生产环境用 .env 覆盖。"""

    PROJECT_NAME: str = "flowmart"
    API_V1_PREFIX: str = "/api/v1"
    DEBUG: bool = True

    # MySQL 连接
    DB_HOST: str = "127.0.0.1"
    DB_PORT: int = 3306
    DB_USER: str = "root"
    DB_PASSWORD: str = "1234560"
    DB_NAME: str = "flowmart"
    DB_ECHO: bool = False

    # 直接给定完整连接串时优先使用它。
    # 用途：切到 SQLite 做本地可视化/单测，或连到其它环境，无需改代码。
    DATABASE_URL: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def database_url(self) -> str:
        """最终连接串：DATABASE_URL 优先，否则按 MySQL 参数拼装。"""
        if self.DATABASE_URL:
            return self.DATABASE_URL
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
