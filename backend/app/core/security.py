"""认证与安全：JWT 签发/校验 + 密码哈希。

为什么不用第三方库？
- 本环境安装 pyjwt/bcrypt 受代理限制不稳，而 HS256 与 PBKDF2 用标准库即可正确实现。
- JWT 用 HMAC-SHA256（HS256），签名 = HMAC(密钥, header.payload)；校验用 `hmac.compare_digest`
  做常量时间比较，避免时序侧信道。
- 密码用 PBKDF2-HMAC-SHA256（10 万次迭代）+ 随机盐，存储格式 `pbkdf2$sha256$<iter>$<salt>$<dk>`。
  明文绝不下库，验证时重算并比较。

令牌里只放 `sub`（用户 id）、`iat`、`exp` 三类claim，不放敏感信息。
"""
import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from fastapi import Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.models.ecommerce import User

ALG = "HS256"
_BEARER = HTTPBearer(auto_error=False)


# ---------------- base64url 工具 ----------------


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


class JWTError(Exception):
    """令牌格式/签名/过期等校验失败的统一异常。"""


# ---------------- JWT ----------------


def create_access_token(sub: int | str, expires_minutes: int | None = None, extra: dict | None = None) -> str:
    """签发 HS256 访问令牌，sub 固定为用户 id（字符串化以便解码）。"""
    now = int(time.time())
    exp = now + (expires_minutes or settings.ACCESS_TOKEN_EXPIRE_MINUTES) * 60
    payload = {"sub": str(sub), "iat": now, "exp": exp}
    if extra:
        payload.update(extra)
    h = _b64u(json.dumps({"alg": ALG, "typ": "JWT"}, separators=(",", ":")).encode("utf-8"))
    p = _b64u(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(settings.SECRET_KEY.encode("utf-8"), f"{h}.{p}".encode("utf-8"), hashlib.sha256).digest()
    return f"{h}.{p}.{_b64u(sig)}"


def decode_access_token(token: str) -> dict:
    """校验签名与过期，返回 payload；任何问题抛 JWTError（统一转成 401）。"""
    try:
        h, p, s = token.split(".")
    except ValueError:
        raise JWTError("令牌格式错误")
    try:
        expected = hmac.new(settings.SECRET_KEY.encode("utf-8"), f"{h}.{p}".encode("utf-8"), hashlib.sha256).digest()
        # 常量时间比较，防止时序攻击；坏 base64 也在此兜住
        if not hmac.compare_digest(expected, _b64d(s)):
            raise JWTError("签名无效")
        payload = json.loads(_b64d(p))
    except (JWTError, ValueError, binascii.Error):
        raise JWTError("签名无效或载荷损坏")
    if "sub" not in payload:
        raise JWTError("缺少用户标识")
    if "exp" not in payload:
        raise JWTError("缺少过期时间")
    if payload["exp"] < int(time.time()):
        raise JWTError("令牌已过期")
    return payload


# ---------------- 密码哈希 ----------------


def hash_password(pw: str, *, iterations: int = 200_000) -> str:
    """PBKDF2-HMAC-SHA256，返回可存储的字符串（迭代次数随算力提升而调大）。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iterations)
    return f"pbkdf2$sha256${iterations}${_b64u(salt)}${_b64u(dk)}"


def verify_password(pw: str, stored: str) -> bool:
    """重算并常量时间比较；stored 为空或格式不对直接判否。"""
    if not stored:
        return False
    try:
        algo, hashname, iters, salt_b64, dk_b64 = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2":
        return False
    try:
        computed = _b64u(hashlib.pbkdf2_hmac(hashname, pw.encode("utf-8"), _b64d(salt_b64), int(iters)))
    except Exception:
        return False
    return hmac.compare_digest(computed, dk_b64)


# ---------------- FastAPI 依赖 ----------------


def set_auth_cookie(response: Response, token: str) -> None:
    """把 JWT 写入 httpOnly Cookie：XSS 无法读取，防御令牌被盗。"""
    response.set_cookie(
        settings.JWT_COOKIE_NAME,
        token,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


def clear_auth_cookie(response: Response) -> None:
    """退出登录：清除 httpOnly Cookie（前端 JS 无法删，必须由后端发指令）。

    必须与 set_auth_cookie 的 secure/samesite 保持一致：否则生产环境
    （COOKIE_SECURE=True、samesite=lax）下浏览器因属性不匹配而清不掉 Cookie，
    导致「退出登录」形同虚设。
    """
    response.delete_cookie(
        settings.JWT_COOKIE_NAME,
        secure=settings.COOKIE_SECURE,
        httponly=True,
        samesite=settings.COOKIE_SAMESITE,
    )


def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    db: Session = Depends(get_db),
) -> User:
    """Bearer 头或 httpOnly Cookie → 当前用户对象。缺失/无效/禁用一律 401。

    优先 Bearer 头（API 客户端 / 测试），浏览器同源请求走 Cookie 作为兜底；
    这样「当前用户」永远来自令牌，前端传来的 user_id 不再被信任，
    从根上消除冒充他人下单/看地址的越权。
    """
    # 优先 Bearer 头，其次 httpOnly Cookie
    token = None
    if creds is not None and creds.scheme.lower() == "bearer":
        token = creds.credentials
    else:
        token = request.cookies.get(settings.JWT_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="未提供有效的身份凭证")
    try:
        payload = decode_access_token(token)
    except JWTError as exc:
        raise HTTPException(status_code=401, detail=f"身份凭证无效：{exc}")
    uid = int(payload["sub"])
    user = db.get(User, uid)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="用户不存在或已禁用")
    return user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """管理员角色闸门：非管理员一律 403。

    用于用户管理、订单流转等「只有后台运营可做的操作」。
    与 get_current_user（身份取自令牌）配合，把越权从接口形状上堵死——
    普通买家既拿不到管理入口，也无法通过改 URL 里的 id 越权。
    """
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return current_user


def ensure_admin_exists(db: Session) -> None:
    """seed / 迁移收尾校验：系统中若无任何管理员，启动即报错，避免「谁都不是管理员」静默锁死。

    与 BOOTSTRAP_ADMIN 配合：即便配置写错了名字（该用户没被 seed 进去），
    也能在启动时暴露，而不是让所有管理接口悄悄不可用。
    """
    admin_count = db.execute(
        select(func.count())
        .select_from(User)
        .where(User.is_admin.is_(True), User.is_active.is_(True))
    ).scalar()
    if not admin_count:
        raise RuntimeError(
            "系统中没有任何管理员账号：请确认 BOOTSTRAP_ADMIN 指向的用户已被创建，"
            "否则所有管理接口将无法使用、系统陷入锁死。"
        )
