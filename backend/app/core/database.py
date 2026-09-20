"""数据库引擎与会话管理。"""
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import settings

def _build_engine():
    """按方言差异化建引擎。

    - MySQL：pool_pre_ping 防止 8 小时空闲被服务端断开后的 "server has gone away"
    - SQLite：check_same_thread=False，否则 FastAPI 的多线程请求会报错
    """
    url = settings.database_url
    if settings.dialect == "sqlite":
        return create_engine(
            url, echo=settings.DB_ECHO, future=True, connect_args={"check_same_thread": False}
        )
    return create_engine(
        url,
        echo=settings.DB_ECHO,
        pool_pre_ping=True,
        pool_recycle=3600,
        future=True,
    )


engine = _build_engine()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


def get_db() -> Generator:
    """FastAPI 依赖：每个请求一个会话，请求结束必定关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
