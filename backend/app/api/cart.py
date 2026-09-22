"""购物车 API。

设计要点：
1. 同一 SKU 重复加入是**累加数量**而不是新增一行 —— 否则同一商品会在列表里出现多次，
   既难看也让「改数量」的语义变得混乱。
2. 结算与清空购物车必须在同一事务：调用 create_order 时传 auto_commit=False，
   全部成功后统一提交，避免出现「订单已生成但购物车还在」。
3. 所有按 id 操作购物车的接口都必须带上 user_id 一起过滤 —— 只按 item_id 查询
   会让任意用户修改甚至删除他人的购物车项。
4. **身份来自令牌**：购物车归属于当前登录用户（get_current_user），不再信任请求体里的
   user_id。之前购物车越权 bug 的根因就是 user_id 由前端随便填，现在主语固定为令牌用户。
"""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user, require_verified_contact
from app.models.ecommerce import CartItem, Sku, User
from app.services.order_service import OrderService
from app.services.workflow_engine import WorkflowError

router = APIRouter(prefix="/cart", tags=["购物车"])


class CartAddIn(BaseModel):
    sku_id: int
    quantity: int = Field(1, ge=1)


class CartUpdateIn(BaseModel):
    quantity: int = Field(..., ge=0)  # 0 表示移除该商品


class CheckoutIn(BaseModel):
    address_id: int | None = None
    item_ids: list[int] | None = None  # 不传表示结算全部


def _owned_item(db: Session, item_id: int, user_id: int) -> CartItem:
    """按 id + user_id 共同定位。

    只按 id 查会导致越权：任何人都能改/删他人的购物车项。
    查不到时统一返回 404，不区分「不存在」和「不属于你」，避免信息泄漏。
    """
    item = (
        db.execute(
            select(CartItem).where(CartItem.id == item_id, CartItem.user_id == user_id)
        )
        .scalars()
        .first()
    )
    if item is None:
        raise HTTPException(status_code=404, detail="购物车项不存在")
    return item


def _serialize(items: list[CartItem], db: Session) -> tuple[list[dict], float]:
    """把购物车项连 SKU 一起序列化，并计算合计。"""
    result = []
    total = Decimal("0.00")
    for it in items:
        sku = db.get(Sku, it.sku_id)
        if sku is None:
            continue  # SKU 已被删除的幽灵行，跳过而不是让整个接口失败
        subtotal = Decimal(str(sku.price)) * it.quantity
        total += subtotal
        result.append(
            {
                "id": it.id,
                "sku_id": sku.id,
                "sku_code": sku.sku_code,
                "product_name": sku.product.name if sku.product else sku.sku_code,
                "spec": sku.spec,
                "price": float(sku.price),
                "quantity": it.quantity,
                "subtotal": float(subtotal),
                "stock": sku.stock,
            }
        )
    return result, float(total)


@router.get("", summary="查看我的购物车")
def list_cart(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    items = (
        db.execute(
            select(CartItem).where(CartItem.user_id == current_user.id).order_by(CartItem.id)
        )
        .scalars()
        .all()
    )
    rows, total = _serialize(items, db)
    return {"user_id": current_user.id, "items": rows, "total": total}


@router.post("", status_code=201, summary="加入购物车")
def add_to_cart(
    payload: CartAddIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    sku = db.get(Sku, payload.sku_id)
    if sku is None:
        raise HTTPException(status_code=404, detail="SKU 不存在")
    if sku.status != "on_sale":
        raise HTTPException(status_code=400, detail="该商品已下架")
    if sku.stock < payload.quantity:
        raise HTTPException(status_code=400, detail=f"库存不足（剩 {sku.stock}）")

    existing = (
        db.execute(
            select(CartItem).where(
                CartItem.user_id == current_user.id, CartItem.sku_id == payload.sku_id
            )
        )
        .scalars()
        .first()
    )
    if existing:
        # 先算后写：若先把数量加上去再校验，超限时虽然抛了异常，
        # 但改动已留在会话里，之后该会话再 commit 就会把越界值写进去
        new_qty = existing.quantity + payload.quantity
        if new_qty > sku.stock:
            db.rollback()
            raise HTTPException(
                status_code=400,
                detail=f"购物车中该商品已达库存上限 {sku.stock}（当前 {existing.quantity}）",
            )
        existing.quantity = new_qty
        item = existing
    else:
        item = CartItem(
            user_id=current_user.id, sku_id=payload.sku_id, quantity=payload.quantity
        )
        db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "sku_id": item.sku_id, "quantity": item.quantity}


@router.patch("/{item_id}", summary="修改数量（0 表示移除）")
def update_quantity(
    item_id: int,
    payload: CartUpdateIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = _owned_item(db, item_id, current_user.id)

    if payload.quantity == 0:
        db.delete(item)
        db.commit()
        return {"id": item_id, "removed": True}

    sku = db.get(Sku, item.sku_id)
    if sku is None:
        # SKU 已被删除，这一行成了幽灵数据：列表里看不见、结算也必然失败，
        # 与其永久残留不如顺手清理
        db.delete(item)
        db.commit()
        raise HTTPException(status_code=404, detail="商品已不存在，已从购物车移除")
    if sku.status != "on_sale":
        raise HTTPException(status_code=400, detail="该商品已下架")
    if payload.quantity > sku.stock:
        raise HTTPException(status_code=400, detail=f"库存不足（剩 {sku.stock}）")

    item.quantity = payload.quantity
    db.commit()
    return {"id": item.id, "quantity": item.quantity}


@router.delete("/{item_id}", summary="移除商品")
def remove_item(
    item_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = _owned_item(db, item_id, current_user.id)
    db.delete(item)
    db.commit()
    return {"id": item_id, "removed": True}


@router.post("/checkout", status_code=201, summary="结算购物车")
def checkout(
    payload: CheckoutIn,
    current_user: User = Depends(require_verified_contact),
    db: Session = Depends(get_db),
):
    """购物车结算：生成订单并清空已结算的商品，两者在同一事务内完成。

    与 orders.create_order 共用同一道「验证闸门」依赖（require_verified_contact），
    避免购物车结算成为绕过验证的下单后门。
    """
    user_id = current_user.id
    stmt = select(CartItem).where(CartItem.user_id == user_id)
    if payload.item_ids:
        stmt = stmt.where(CartItem.id.in_(payload.item_ids))
    items = db.execute(stmt).scalars().all()
    if not items:
        raise HTTPException(status_code=400, detail="购物车为空，无法结算")

    # 传入了指定 id 却有一部分没命中（不存在或不属于该用户），
    # 必须报错而不是静默只结算命中的部分，否则用户以为全结算了
    if payload.item_ids:
        missing = set(payload.item_ids) - {i.id for i in items}
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"以下购物车项不存在或不属于该用户：{sorted(missing)}",
            )

    svc = OrderService(db)
    try:
        # auto_commit=False：先不提交，等购物车清空后一起提交
        order = svc.create_order(
            user_id=user_id,
            items=[{"sku_id": i.sku_id, "quantity": i.quantity} for i in items],
            address_id=payload.address_id,
            auto_commit=False,
        )
    except (ValueError, WorkflowError) as exc:
        # 库存不足、SKU 下架、流程未发布等，都要回滚并转成客户端可读的错误；
        # 地址相关的越权/不存在统一 404（与 orders.py 约定一致），不泄露目标是否存在
        db.rollback()
        status = 404 if "收货地址" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc

    try:
        for i in items:
            db.delete(i)
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "order_id": order.id,
        "order_no": order.order_no,
        "pay_amount": float(order.pay_amount),
        "status": order.status,
    }
