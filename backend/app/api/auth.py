"""认证接口：注册、登录、当前用户、退出登录。

登录/注册在返回 JWT（Bearer，便于 API 客户端）的同时写入 httpOnly Cookie，
浏览器同源请求由 Cookie 携带令牌，前端不再用 localStorage 存明文令牌（抗 XSS 窃取）。
所有资源接口通过 Depends(get_current_user) 解析出当前用户，不再信任请求体里的 user_id。
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from typing import Literal

from app.core.config import settings
from app.core.database import get_db
from app.core.ratelimit import login_limiter
from app.core.security import (
    JWTError,
    clear_auth_cookie,
    clear_refresh_cookie,
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    get_current_user,
    hash_password,
    set_auth_cookie,
    set_refresh_cookie,
    verify_password,
)
from app.core.verification import VerificationError, confirm_code, request_code
from app.models.ecommerce import User

router = APIRouter(prefix="/auth", tags=["认证"])


class RegisterIn(BaseModel):
    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=6, max_length=128)
    nickname: str = ""
    phone: str = ""
    email: str = ""


class LoginIn(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


@router.post("/register", status_code=201, summary="注册并直接登录")
def register(payload: RegisterIn, response: Response, db: Session = Depends(get_db)):
    if db.scalar(select(User).where(User.username == payload.username)):
        raise HTTPException(status_code=400, detail=f"用户名 {payload.username} 已存在")
    user = User(
        username=payload.username,
        nickname=payload.nickname,
        phone=payload.phone,
        email=payload.email,
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_access_token(user.id)
    refresh = create_refresh_token(user.id)
    set_auth_cookie(response, token)
    set_refresh_cookie(response, refresh)
    return {
        "id": user.id,
        "username": user.username,
        "access_token": token,
        "refresh_token": refresh,
        "token_type": "bearer",
    }


@router.post("/login", summary="登录换取令牌")
def login(payload: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    # 先查限流：同一 (IP, 用户名) 窗口内失败过多直接拒，阻断密码爆破
    if login_limiter.is_blocked(request, payload.username):
        retry = login_limiter.retry_after(request, payload.username)
        raise HTTPException(
            status_code=429,
            detail=f"登录失败次数过多，请 {retry} 秒后再试",
            headers={"Retry-After": str(retry)},
        )
    user = db.scalar(select(User).where(User.username == payload.username))
    # 用户名不存在与密码错误返回同样的提示，避免被用来探测哪些用户名已注册
    if user is None or not verify_password(payload.password, user.password_hash):
        login_limiter.register_failure(request, payload.username)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not user.is_active:
        # 禁用账号不计失败次数：它本就不是「猜密码」，反复计数只会白白占窗口，
        # 但仍拒绝登录。注意这里不 reset——禁用状态与爆破无关。
        raise HTTPException(status_code=401, detail="该用户已被禁用")
    # 登录成功清空失败计数，避免正常用户刚改完密码就被旧失败数误伤
    login_limiter.reset(request, payload.username)
    token = create_access_token(user.id)
    refresh = create_refresh_token(user.id)
    set_auth_cookie(response, token)
    set_refresh_cookie(response, refresh)
    return {
        "access_token": token,
        "refresh_token": refresh,
        "token_type": "bearer",
        "user_id": user.id,
    }


@router.post("/logout", summary="退出登录（清除 httpOnly Cookie）")
def logout(response: Response):
    # 前端 JS 无法删除 httpOnly Cookie，必须由后端发删除指令；访问 + 刷新令牌一并清除
    clear_auth_cookie(response)
    clear_refresh_cookie(response)
    return {"detail": "已退出登录"}


@router.post("/refresh", summary="用刷新令牌换发访问令牌（静默续期）")
def refresh(request: Request, response: Response, db: Session = Depends(get_db)):
    """访问令牌过期时由前端静默调用：读刷新令牌（浏览器走 httpOnly Cookie，API 客户端走 Bearer 头），
    校验通过后换发新访问令牌并刷新访问 Cookie。不轮转刷新令牌——无服务端状态时的伪轮转反而更危险，
    刷新令牌的长期有效性由 SameSite Cookie + httpOnly 共同保护。"""
    token = request.cookies.get(settings.REFRESH_TOKEN_COOKIE_NAME)
    # API 客户端（非浏览器）可把刷新令牌放在 Bearer 头；浏览器用不到
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="缺少刷新令牌")
    try:
        claims = decode_refresh_token(token)
    except JWTError as exc:
        raise HTTPException(status_code=401, detail=f"刷新令牌无效：{exc}")
    uid = int(claims["sub"])
    user = db.get(User, uid)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="用户不存在或已禁用")
    new_access = create_access_token(user.id)
    set_auth_cookie(response, new_access)
    return {"access_token": new_access, "token_type": "bearer"}


@router.get("/me", summary="当前登录用户")
def me(user: User = Depends(get_current_user)):
    return {
        "id": user.id,
        "username": user.username,
        "nickname": user.nickname,
        "phone": user.phone,
        "email": user.email or "",
        "email_verified": user.email_verified,
        "phone_verified": user.phone_verified,
        "is_active": user.is_active,
    }


class VerificationSendIn(BaseModel):
    channel: Literal["email", "phone"]
    target: str = Field(..., min_length=1, max_length=255)


class VerificationConfirmIn(BaseModel):
    channel: Literal["email", "phone"]
    target: str = Field(..., min_length=1, max_length=255)
    code: str = Field(..., min_length=1, max_length=16)


@router.post("/verification/send", summary="申请邮箱/手机验证码")
def send_verification(
    payload: VerificationSendIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """申请一次性验证码。开发环境（OTP_DEV_RETURN_CODE=True）会在响应里回传 dev_code 便于联调，
    生产必须关掉该开关（配置校验会强制）。"""
    try:
        code = request_code(db, user, payload.channel, payload.target)
    except VerificationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    resp = {"sent": True, "channel": payload.channel, "expires_in": settings.OTP_TTL_SECONDS}
    if settings.OTP_DEV_RETURN_CODE:
        resp["dev_code"] = code
    return resp


@router.post("/verification/confirm", summary="确认验证码并标记渠道已验证")
def confirm_verification(
    payload: VerificationConfirmIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """确认成功后：email 渠道会绑定邮箱并置 email_verified；phone 渠道置 phone_verified。"""
    try:
        confirm_code(db, user, payload.channel, payload.target, payload.code)
    except VerificationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return {
        "verified": True,
        "channel": payload.channel,
        "email": user.email or "",
        "email_verified": user.email_verified,
        "phone_verified": user.phone_verified,
    }
