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
    get_optional_current_user,
    hash_password,
    password_changed_after_token,
    set_auth_cookie,
    set_refresh_cookie,
    utcnow_naive,
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
    # 验证闸门：未验证邮箱或手机不允许登录（防止匿名/未验证账号进入系统）。
    # 未验证用户可通过 /auth/verification/send + /confirm 自助验证（支持凭账号密码自证身份，
    # 无需先登录），验证后再登录即可，因此不会被永久锁死。
    if not (user.email_verified or user.phone_verified):
        raise HTTPException(
            status_code=403,
            detail="该账号尚未验证邮箱或手机，请先完成验证后再登录",
        )
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
    # 刷新令牌同样受改密约束：否则改密后拿旧刷新令牌仍能无限续期，会话失效形同虚设
    if password_changed_after_token(user, claims.get("iat")):
        raise HTTPException(status_code=401, detail="密码已修改，请重新登录")
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
    # 未登录自助验证：未验证用户无法调用需令牌的接口，可用账号密码自证身份来申请验证码
    username: str | None = None
    password: str | None = None


class VerificationConfirmIn(BaseModel):
    channel: Literal["email", "phone"]
    target: str = Field(..., min_length=1, max_length=255)
    code: str = Field(..., min_length=1, max_length=16)
    # 未登录自助验证：同上，确认时也用账号密码自证身份
    username: str | None = None
    password: str | None = None


def _resolve_verification_user(payload, token_user: User | None, db: Session) -> User:
    """解析要验证的用户：已登录优先用令牌；未登录必须凭账号密码自证身份。

    否则未验证用户会陷入死锁：验证接口要令牌 -> 没登录拿不到令牌 -> 被登录闸门挡住永远无法验证。
    """
    if token_user is not None:
        return token_user
    if payload.username and payload.password:
        u = db.scalar(select(User).where(User.username == payload.username))
        if u is None or not verify_password(payload.password, u.password_hash):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        if not u.is_active:
            raise HTTPException(status_code=401, detail="该用户已被禁用")
        return u
    raise HTTPException(status_code=401, detail="请先登录或提供账号密码以自助验证")


@router.post("/verification/send", summary="申请邮箱/手机验证码")
def send_verification(
    payload: VerificationSendIn,
    user: User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    """申请一次性验证码。开发环境（OTP_DEV_RETURN_CODE=True）会在响应里回传 dev_code 便于联调，
    生产必须关掉该开关（配置校验会强制）。

    身份解析：已登录用令牌；未登录凭账号密码自证（详见 _resolve_verification_user），
    使得未验证用户也能在登录前完成自助验证。"""
    try:
        target_user = _resolve_verification_user(payload, user, db)
        code = request_code(db, target_user, payload.channel, payload.target)
    except VerificationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    resp = {"sent": True, "channel": payload.channel, "expires_in": settings.OTP_TTL_SECONDS}
    if settings.OTP_DEV_RETURN_CODE:
        resp["dev_code"] = code
    return resp


@router.post("/verification/confirm", summary="确认验证码并标记渠道已验证")
def confirm_verification(
    payload: VerificationConfirmIn,
    user: User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    """确认成功后：email 渠道会绑定邮箱并置 email_verified；phone 渠道置 phone_verified。

    身份解析同 send（已登录用令牌 / 未登录凭账号密码自证）。"""
    try:
        target_user = _resolve_verification_user(payload, user, db)
        confirm_code(db, target_user, payload.channel, payload.target, payload.code)
    except VerificationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    # 用解析出的 target_user 回显验证状态，而非可选令牌 user——
    # 未登录自助验证时 user 为 None，若回显 user.* 会触发 500。
    return {
        "verified": True,
        "channel": payload.channel,
        "email": target_user.email or "",
        "email_verified": target_user.email_verified,
        "phone_verified": target_user.phone_verified,
    }


class PasswordResetRequestIn(BaseModel):
    username: str = Field(..., min_length=1)
    channel: Literal["email", "phone"]


class PasswordResetConfirmIn(BaseModel):
    username: str = Field(..., min_length=1)
    channel: Literal["email", "phone"]
    code: str = Field(..., min_length=1, max_length=16)
    new_password: str = Field(..., min_length=6, max_length=128)


class ChangePasswordIn(BaseModel):
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=6, max_length=128)


def _reset_target(user: User, channel: str) -> str:
    """取该渠道在库中已验证的联系方式作为验证码投递地址；该渠道未验证则报错。

    验证码只发往账号自身已验证的联系方式，而非客户端随意填写的 target ——
    既能避免钓鱼/误填，也天然要求「先验证过联系方式」才能找回密码。
    """
    if channel == "email":
        if not user.email_verified:
            raise HTTPException(status_code=400, detail="该邮箱尚未验证，无法用于找回密码")
        return user.email
    if not user.phone_verified:
        raise HTTPException(status_code=400, detail="该手机尚未验证，无法用于找回密码")
    return user.phone


@router.post("/password/reset/send", summary="申请找回密码验证码（无需旧密码）")
def password_reset_send(payload: PasswordResetRequestIn, db: Session = Depends(get_db)):
    """账号找回：对已验证邮箱/手机申请验证码，无需提供旧密码（面向忘记密码 / 生产空密码账号）。

    身份 = 用户名 + 控制已验证联系方式（由 OTP 证明），因此不需要旧密码。
    验证码发往该渠道在库中的已验证联系方式，而非客户端填写的 target。
    """
    user = db.scalar(select(User).where(User.username == payload.username))
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="账号不存在或已禁用")
    target = _reset_target(user, payload.channel)
    try:
        code = request_code(db, user, payload.channel, target, purpose="reset")
    except VerificationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    resp = {"sent": True, "channel": payload.channel, "expires_in": settings.OTP_TTL_SECONDS}
    if settings.OTP_DEV_RETURN_CODE:
        resp["dev_code"] = code
    return resp


@router.post("/password/reset/confirm", summary="凭验证码重置密码")
def password_reset_confirm(payload: PasswordResetConfirmIn, db: Session = Depends(get_db)):
    """确认找回验证码后设置新密码。身份 = 用户名 + 控制已验证联系方式（OTP 证明），无需旧密码。

    与验证联系方式用的 OTP 通过 purpose="reset" 隔离：找回码不能拿去当验证用，反之亦然。
    """
    user = db.scalar(select(User).where(User.username == payload.username))
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="账号不存在或已禁用")
    target = _reset_target(user, payload.channel)
    try:
        confirm_code(db, user, payload.channel, target, payload.code, purpose="reset")
    except VerificationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    user.password_hash = hash_password(payload.new_password)
    # 打改密时间戳：签发时间早于此刻的令牌（访问 + 刷新）全部失效，
    # 否则持旧会话/旧刷新令牌的人仍能继续访问，找回密码就失去了意义。
    user.pwd_changed_at = utcnow_naive()
    db.commit()
    return {"reset": True, "username": user.username}


@router.patch("/me/password", summary="登录用户修改自己的密码")
def change_password(
    payload: ChangePasswordIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """已登录用户修改自身密码：需提供正确的原密码，防他人借已登录会话偷偷改密。

    这补上了原先只有管理员能经 users.update_user 改密码的缺口——普通用户此前无法自助改密。
    改错原密码复用了登录限流（同一 IP + 用户名窗口计数），避免被拿来循环试原密码，
    也避免反复触发 20 万次 PBKDF2 造成 CPU 放大。
    """
    if login_limiter.is_blocked(request, user.username):
        retry = login_limiter.retry_after(request, user.username)
        raise HTTPException(
            status_code=429,
            detail=f"修改密码失败次数过多，请 {retry} 秒后再试",
            headers={"Retry-After": str(retry)},
        )
    if not verify_password(payload.old_password, user.password_hash):
        login_limiter.register_failure(request, user.username)
        raise HTTPException(status_code=400, detail="原密码错误")
    login_limiter.reset(request, user.username)
    user.password_hash = hash_password(payload.new_password)
    # 同 reset：改密作废此前签发的所有令牌
    user.pwd_changed_at = utcnow_naive()
    db.commit()
    return {"changed": True}
