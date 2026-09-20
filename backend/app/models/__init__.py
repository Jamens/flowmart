"""模型包：导入本包即注册全部 ORM 表，供 Base.metadata.create_all 使用。"""
from app.core.database import Base
from app.models import ecommerce, workflow  # noqa: F401

__all__ = ["Base", "ecommerce", "workflow"]
