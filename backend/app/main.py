"""flowmart 应用入口。"""
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    admin_db,
    auth,
    cart,
    categories,
    orders,
    payments,
    products,
    stats,
    uploads,
    users,
    workflows,
)
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


def _is_secure(request: Request) -> bool:
    """判断请求是否 https，考虑反向代理。

    直连时取 `request.url.scheme`（TLS 真实终止在应用侧）。
    反代后面（`TRUST_PROXY=True`）时取 `X-Forwarded-Proto`：代理在 TLS 终止点
    已把真实协议写进该头（Nginx `proxy_set_header X-Forwarded-Proto $scheme`）。
    不开 `TRUST_PROXY` 时绝不信这个头——否则攻击者给自己塞个
    `X-Forwarded-Proto: https` 就能骗过 HSTS / 强制 HTTPS 判定。
    """
    if settings.TRUST_PROXY:
        proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        if proto:
            return proto == "https"
    return request.url.scheme == "https"


@app.middleware("http")
async def _tls_enforcement(request: Request, call_next):
    """HTTPS 强制跳转 + HSTS。

    - `ENFORCE_HTTPS`：非 https 请求 307 跳到同路径的 https 版本（基于请求 host）。
      反代后**必须**同时 `TRUST_PROXY=True`（启动已校验），否则代理转发来的 http 请求
      会被无限重定向。
    - 健康检查 `/health` 跳过跳转：反代内部通常走 http 探活，跳了反而让探针误判失败。
    - HSTS：https 响应上加 `Strict-Transport-Security`（`HSTS_MAX_AGE>0` 时）。
    """
    if settings.ENFORCE_HTTPS and not _is_secure(request) and request.url.path != "/health":
        https_url = request.url.replace(scheme="https")
        return RedirectResponse(url=str(https_url), status_code=307)
    resp = await call_next(request)
    if settings.HSTS_MAX_AGE > 0 and _is_secure(request):
        resp.headers["Strict-Transport-Security"] = f"max-age={settings.HSTS_MAX_AGE}; includeSubDomains"
    return resp

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
app.include_router(stats.router, prefix=api)
# 支付：发起支付走鉴权，渠道回调无鉴权（安全性靠验签，见 api/payments.py）
app.include_router(payments.router, prefix=api)

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


# 健康检查**必须注册在前端挂载之前**：下面的 Mount 按前缀匹配 "/"，
# 会连 "/health" 一起吞掉（实测返回 404，导致镜像 HEALTHCHECK 永远失败、
# 容器被判不健康）。同理它也要在 /uploads 挂载之前——不过那个是具体前缀，不冲突。
@app.get("/health", tags=["system"], summary="健康检查")
def health() -> dict:
    return {
        "status": "ok",
        "project": settings.PROJECT_NAME,
        "dialect": settings.dialect,
        "deploy_env": settings.DEPLOY_ENV,
    }


# 容器镜像会把前端构建产物放进 FRONTEND_DIST；存在时以根路径提供。
# 刻意做成**同源部署**：一个端口、一个域，既不用配 CORS，
# Cookie 的 Secure / SameSite 也不会因为跨站而失效（见 config.py 的 COOKIE_* 说明）。
# 开发时该目录不存在，前端走 vite dev server，两边互不影响。
# 放在所有 API 路由**之后**挂载：Starlette 按注册顺序匹配，
# 先注册的 API 路由优先，"/" 只兜底前端资源。
_FRONTEND_DIST = Path(settings.FRONTEND_DIST)
if _FRONTEND_DIST.is_dir():
    app.mount(
        "/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend"
    )
