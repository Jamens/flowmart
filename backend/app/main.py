"""flowmart 应用入口。"""
from fastapi import FastAPI

from app.core.config import settings

app = FastAPI(
    title="flowmart",
    description="电商后端 + 可配置工作流引擎",
    version="0.1.0",
    debug=settings.DEBUG,
)


@app.get("/health", tags=["system"], summary="健康检查")
def health() -> dict:
    return {"status": "ok", "project": settings.PROJECT_NAME}
