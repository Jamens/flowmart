"""flowmart 应用入口。"""
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import admin_db, auth, cart, categories, orders, products, uploads, users, workflows
from app.core.config import settings

app = FastAPI(
    title="flowmart",
    description="电商后端 + 可配置工作流引擎",
    version="0.1.0",
    debug=settings.DEBUG,
)

# 跨域：仅放行配置中的已知前端源（settings.CORS_ORIGINS），禁止 `*`，配合 credentials 使用
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api = settings.API_V1_PREFIX
app.include_router(products.router, prefix=api)
app.include_router(categories.router, prefix=api)
app.include_router(cart.router, prefix=api)
app.include_router(users.router, prefix=api)
app.include_router(orders.router, prefix=api)
app.include_router(workflows.router, prefix=api)
app.include_router(admin_db.router, prefix=api)
app.include_router(auth.router, prefix=api)
app.include_router(uploads.router, prefix=api)

# 上传目录以静态资源挂载：**读取不鉴权**——商品图片需要未登录也能看（商城页）。
# 上传本身才是管理员操作（见 api/uploads.py 的 require_admin），两者分工不同：
# 上传收紧、读放开，否则商城页的封面图对游客全是 401。
_UPLOAD_DIR = Path(settings.UPLOAD_DIR).resolve()
# 只读挂载点必须与源码分离：UPLOAD_DIR 配成项目根 / backend / "." 会把 .env、
# 数据库、源码在**没有任何告警**的情况下对全网匿名可读。启动时 fail loud。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]  # backend/app/main.py -> 项目根
for _bad in (_PROJECT_ROOT, _PROJECT_ROOT / "backend"):
    if _UPLOAD_DIR == _bad.resolve():
        raise RuntimeError(
            f"UPLOAD_DIR 不能指向 {_bad}：该目录会被匿名公开读取（含 .env / 源码 / 数据库）"
        )
_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
# follow_symlink 显式保持默认 False：改成 True 会让 StaticFiles 用 abspath 而非
# realpath，软链可以指到挂载目录之外——目录穿越防护随之失效。
app.mount(
    settings.UPLOAD_URL_PREFIX,
    StaticFiles(directory=str(_UPLOAD_DIR), follow_symlink=False),
    name="uploads",
)


@app.middleware("http")
async def _upload_security_headers(request: Request, call_next):
    """给上传目录的响应补 `X-Content-Type-Options: nosniff`。

    浏览器对 image/* 不会 sniff 成 HTML，所以这不是「修漏洞」，而是**不把安全性
    押在下游 Content-Type 永远正确**这个不可验证的假设上：将来换 CDN、改静态路由、
    或部署机 mime 表差异都可能让 Content-Type 退化成 application/octet-stream，
    那时 nosniff 就是唯一兜底。
    """
    resp = await call_next(request)
    if request.url.path.startswith(settings.UPLOAD_URL_PREFIX):
        resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@app.get("/health", tags=["system"], summary="健康检查")
def health() -> dict:
    return {"status": "ok", "project": settings.PROJECT_NAME, "dialect": settings.dialect}
