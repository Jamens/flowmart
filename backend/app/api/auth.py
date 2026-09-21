"""认证接口：注册、登录、当前用户、退出登录。

登录/注册在返回 JWT（Bearer，便于 API 客户端）的同时写入 httpOnly Cookie，
浏览器同源请求由 Cookie 携带令牌，前端不再用 localStorage 存明文令牌（抗 XSS 窃取）。
所有资源接口通过 Depends(get_current_user) 解析出当前用户，不再信任请求体里的 user_id。
"""
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import (
    clear_auth_cookie,
    create_access_token,
    get_current_user,
    hash_password,
    set_auth_cookie,
    verify_password,
)
from app.models.ecommerce import User

router = APIRouter(prefix="/auth", tags=["认证"])


class RegisterIn(BaseModel):
    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=6, max_length=128)
    nickname: str = ""
    phone: str = ""


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
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_access_token(user.id)
    set_auth_cookie(response, token)
    return {
        "id": user.id,
        "username": user.username,
        "access_token": token,
        "token_type": "bearer",
    }


@router.post("/login", summary="登录换取令牌")
def login(payload: LoginIn, response: Response, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == payload.username))
    # 用户名不存在与密码错误返回同样的提示，避免被用来探测哪些用户名已注册
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="该用户已被禁用")
    token = create_access_token(user.id)
    set_auth_cookie(response, token)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user_id": user.id,
    }


@router.post("/logout", summary="退出登录（清除 httpOnly Cookie）")
def logout(response: Response):
    # 前端 JS 无法删除 httpOnly Cookie，必须由后端发删除指令
    clear_auth_cookie(response)
    return {"detail": "已退出登录"}


@router.get("/me", summary="当前登录用户")
def me(user: User = Depends(get_current_user)):
    return {
        "id": user.id,
        "username": user.username,
        "nickname": user.nickname,
        "phone": user.phone,
        "is_active": user.is_active,
    }
