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
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
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
    """校验签名与过期，返回 payload；任何问题抛 JWTError。"""
    try:
        h, p, s = token.split(".")
    except ValueError:
        raise JWTError("令牌格式错误")
    expected = hmac.new(settings.SECRET_KEY.encode("utf-8"), f"{h}.{p}".encode("utf-8"), hashlib.sha256).digest()
    # 常量时间比较，防止时序攻击
    if not hmac.compare_digest(expected, _b64d(s)):
        raise JWTError("签名无效")
    try:
        payload = json.loads(_b64d(p))
    except Exception:
        raise JWTError("载荷解析失败")
    if payload.get("exp") is not None and payload["exp"] < int(time.time()):
        raise JWTError("令牌已过期")
    return payload


# ---------------- 密码哈希 ----------------


def hash_password(pw: str, *, iterations: int = 100_000) -> str:
    """PBKDF2-HMAC-SHA256，返回可存储的字符串。"""
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


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    db: Session = Depends(get_db),
) -> User:
    """Bearer 令牌 → 当前用户对象。缺失/无效/禁用一律 401。

    所有需要身份的接口都 Depends 它；这样「当前用户」永远来自令牌，
    前端传来的 user_id 不再被信任，从根上消除冒充他人下单/看地址的越权。
    """
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="未提供有效的身份凭证")
    try:
        payload = decode_access_token(creds.credentials)
    except JWTError as exc:
        raise HTTPException(status_code=401, detail=f"身份凭证无效：{exc}")
    uid = int(payload["sub"])
    user = db.get(User, uid)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="用户不存在或已禁用")
    return user
