"""用户与收货地址 API。

两个设计决策：

1. **删除用户 = 禁用，不是物理删除。**
   orders.user_id 是指向 users 的外键，物理删除会破坏历史订单；
   电商系统里用户数据要保留，用 is_active 标记即可。

2. **地址接口强制归属校验**：地址归属当前登录用户（get_current_user），
   路径里的 user_id 必须等于令牌用户，否则一律 404。
   这样「越权访问他人地址」在接口形状上就不可能发生 —— 从购物车越权 bug 学到的教训。

RBAC（角色权限，已落地）：
- 用户管理（建/列/禁用账号）与订单流转推进仅管理员可执行（require_admin 闸门）；
- 普通买家只能改**自己**的资料、只看**自己**的订单；
- 越权访问他人资源统一返回 404，不泄露「该用户是否存在」。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.pagination import apply_pagination, total_count
from app.core.security import get_current_user, require_admin
from app.models.ecommerce import Address, User

router = APIRouter(prefix="/users", tags=["用户与地址"])


class UserIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field("", min_length=0, max_length=128)  # 留空则不设置密码
    nickname: str = ""
    phone: str = ""


class UserUpdateIn(BaseModel):
    nickname: str | None = None
    phone: str | None = None
    is_active: bool | None = None


class AddressIn(BaseModel):
    receiver: str = Field(..., min_length=1, max_length=64)
    phone: str = Field(..., min_length=1, max_length=20)
    province: str = ""
    city: str = ""
    district: str = ""
    detail: str = ""
    is_default: bool = False


class AddressUpdateIn(BaseModel):
    receiver: str | None = None
    phone: str | None = None
    province: str | None = None
    city: str | None = None
    district: str | None = None
    detail: str | None = None
    is_default: bool | None = None


def _get_user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


def _owned_address(db: Session, user_id: int, address_id: int) -> Address:
    """user_id 与 address_id 联合定位，防止越权访问他人地址。"""
    addr = (
        db.execute(
            select(Address).where(Address.id == address_id, Address.user_id == user_id)
        )
        .scalars()
        .first()
    )
    if addr is None:
        raise HTTPException(status_code=404, detail="收货地址不存在")
    return addr


def _assert_owner(current_user: User, user_id: int) -> None:
    """地址接口：路径 user_id 必须等于令牌用户，否则 404（不泄露「该用户是否存在」）。"""
    if current_user.id != user_id:
        raise HTTPException(status_code=404, detail="收货地址不存在")


def _set_default_address(db: Session, user_id: int, target: Address) -> None:
    """把某个地址设为默认，同时清除该用户其它地址的默认标记。

    默认地址只能有一个，这是数据层面的业务规则，必须在写入时维护，
    否则查询「默认地址」会出现多条，行为不确定。
    """
    if not target.is_default:
        return
    others = (
        db.execute(
            select(Address).where(Address.user_id == user_id, Address.id != target.id)
        )
        .scalars()
        .all()
    )
    for a in others:
        a.is_default = False


# ---------------- 用户 ----------------


@router.get("", summary="用户列表（仅管理员）")
def list_users(
    active_only: bool = False,
    # limit=0 表示不分页（返回全部）
    limit: int = 0,
    offset: int = 0,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    stmt = select(User)
    if active_only:
        stmt = stmt.where(User.is_active.is_(True))
    total = total_count(db, stmt)
    users = db.execute(apply_pagination(stmt.order_by(User.id), limit, offset)).scalars().all()
    return {
        "items": [
            {
                "id": u.id,
                "username": u.username,
                "nickname": u.nickname,
                "phone": u.phone,
                "is_active": u.is_active,
            }
            for u in users
        ],
        "total": total,
    }


@router.post("", status_code=201, summary="创建用户（仅管理员）")
def create_user(
    payload: UserIn,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    from app.core.security import hash_password

    exist = (
        db.execute(select(User).where(User.username == payload.username))
        .scalars()
        .first()
    )
    if exist:
        raise HTTPException(status_code=400, detail=f"用户名 {payload.username} 已存在")
    user = User(
        username=payload.username,
        nickname=payload.nickname,
        phone=payload.phone,
        password_hash=hash_password(payload.password) if payload.password else "",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"id": user.id, "username": user.username}


@router.get("/{user_id}", summary="用户详情（本人或管理员）")
def get_user(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # 注意：不在此返回收货地址 —— 地址含 receiver/手机/详细地址等敏感信息，
    # 应通过归属校验的 /users/{id}/addresses 获取，避免任何登录用户读到他人地址。
    # 他人资料对非管理员不可见：统一 404，不泄露「该用户是否存在」。
    u = _get_user(db, user_id)
    if current_user.id != user_id and not current_user.is_admin:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {
        "id": u.id,
        "username": u.username,
        "nickname": u.nickname,
        "phone": u.phone,
        "is_active": u.is_active,
    }


@router.patch("/{user_id}", summary="更新用户（本人改资料 / 管理员可改全部）")
def update_user(
    user_id: int,
    payload: UserUpdateIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    u = _get_user(db, user_id)
    # 本人：仅可改昵称/手机；管理员：可改昵称/手机/启用状态；其他人 404。
    if current_user.id == user_id:
        allowed = ("nickname", "phone")
    elif current_user.is_admin:
        allowed = ("nickname", "phone", "is_active")
    else:
        raise HTTPException(status_code=404, detail="用户不存在")

    # 与 DELETE 闸门统一：不能禁用当前登录的管理员账号（避免自己把自己锁死）。
    # 禁用其他管理员仍允许，与 delete_user 行为保持一致（仅保护当前管理员）。
    if u.is_admin and current_user.id == user_id and payload.is_active is False:
        raise HTTPException(status_code=400, detail="不能禁用当前登录的管理员账号")

    for field in allowed:
        value = getattr(payload, field)
        if value is not None:
            setattr(u, field, value)
    db.commit()
    return {"id": u.id, "nickname": u.nickname, "is_active": u.is_active}


@router.delete("/{user_id}", summary="禁用用户（软删除，仅管理员）")
def delete_user(
    user_id: int,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """不物理删除：订单等历史数据仍引用该用户。仅管理员可执行。"""
    u = _get_user(db, user_id)
    if u.id == current_user.id:
        raise HTTPException(status_code=400, detail="不能禁用当前登录的管理员账号")
    u.is_active = False
    db.commit()
    return {"id": u.id, "is_active": False}


# ---------------- 收货地址 ----------------


@router.get("/{user_id}/addresses", summary="用户的收货地址列表")
def list_addresses(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _assert_owner(current_user, user_id)
    rows = (
        db.execute(select(Address).where(Address.user_id == user_id).order_by(Address.id))
        .scalars()
        .all()
    )
    return [
        {
            "id": a.id,
            "receiver": a.receiver,
            "phone": a.phone,
            "province": a.province,
            "city": a.city,
            "district": a.district,
            "detail": a.detail,
            "is_default": a.is_default,
        }
        for a in rows
    ]


@router.post("/{user_id}/addresses", status_code=201, summary="新增收货地址")
def create_address(
    user_id: int,
    payload: AddressIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _assert_owner(current_user, user_id)
    addr = Address(user_id=user_id, **payload.model_dump())
    db.add(addr)
    db.flush()
    _set_default_address(db, user_id, addr)
    db.commit()
    db.refresh(addr)
    return {"id": addr.id, "is_default": addr.is_default}


@router.patch("/{user_id}/addresses/{address_id}", summary="修改收货地址")
def update_address(
    user_id: int,
    address_id: int,
    payload: AddressUpdateIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _assert_owner(current_user, user_id)
    addr = _owned_address(db, user_id, address_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(addr, field, value)
    db.flush()
    _set_default_address(db, user_id, addr)
    db.commit()
    return {"id": addr.id, "is_default": addr.is_default}


@router.delete("/{user_id}/addresses/{address_id}", summary="删除收货地址")
def delete_address(
    user_id: int,
    address_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _assert_owner(current_user, user_id)
    addr = _owned_address(db, user_id, address_id)
    db.delete(addr)
    db.commit()
    return {"id": address_id, "removed": True}
