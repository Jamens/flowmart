# flowmart 部署镜像：后端 + 前端构建产物，**单容器同源部署**。
#
# 为什么前端也打进同一个镜像：前端用的是相对路径 /api/v1（见 frontend/src/api.js），
# 同源部署下不需要 CORS，Cookie 的 Secure / SameSite 也不会因跨站而失效。
# 少一个 nginx，就少一整套反向代理与容器内 DNS 解析的坑。
# 开发时前端仍走 vite dev server（5173），本镜像只用于部署，两边互不影响。

# ---------- 阶段 1：构建前端 ----------
FROM node:22-alpine AS frontend-build

WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
# ci 而非 install：严格按 lockfile 装，避免不同机器装出不同版本导致「本地好的线上炸了」
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---------- 阶段 2：运行后端 ----------
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖单独一层：requirements.txt 不变时复用缓存，改代码不必重装依赖
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
# 前端产物放到 config.FRONTEND_DIST 的默认位置（PROJECT_ROOT/static）
COPY --from=frontend-build /src/dist ./static/

# 上传目录必须在镜像里先建好并授权：容器以非 root 运行，
# 等到运行时再创建会撞上「/app 属主是 root」的权限错误。
RUN useradd -m -u 10001 flowmart \
    && mkdir -p /app/uploads \
    && chown -R flowmart:flowmart /app/uploads

# 以非 root 运行：即便容器被攻破，拿到的也只是低权限用户
USER flowmart

# uvicorn 需要 app 包在 Python 路径上，因此工作目录切到 backend/
WORKDIR /app/backend

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; raise SystemExit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
