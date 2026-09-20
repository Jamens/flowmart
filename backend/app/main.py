"""flowmart 应用入口。"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import admin_db, orders, products, workflows
from app.core.config import settings

app = FastAPI(
    title="flowmart",
    description="电商后端 + 可配置工作流引擎",
    version="0.1.0",
    debug=settings.DEBUG,
)

# 前端开发服务器（Vite 5173）与后端不同源，必须放开跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api = settings.API_V1_PREFIX
app.include_router(products.router, prefix=api)
app.include_router(orders.router, prefix=api)
app.include_router(workflows.router, prefix=api)
app.include_router(admin_db.router, prefix=api)


@app.get("/health", tags=["system"], summary="健康检查")
def health() -> dict:
    return {"status": "ok", "project": settings.PROJECT_NAME, "dialect": settings.dialect}
