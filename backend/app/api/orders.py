"""订单 API。

流转动作统一走 POST /orders/{id}/actions/{event}：
新增流程事件时不需要改 API 代码，由服务层与流程定义决定支持哪些 event。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.database import get_db
from app.models.ecommerce import Order
from app.services.order_service import OrderService
from app.services.workflow_engine import WorkflowError

router = APIRouter(prefix="/orders", tags=["订单"])


class OrderItemIn(BaseModel):
    sku_id: int
    quantity: int = Field(1, ge=1)


class OrderCreateIn(BaseModel):
    user_id: int
    items: list[OrderItemIn] = Field(..., min_length=1)
    address_id: int | None = None
    remark: str = ""


class ActionIn(BaseModel):
    operator: str = "admin"
    comment: str = ""


def _safe(fn, default):
    """执行可能因缺少流程实例而失败的调用，失败时返回默认值而非抛错。"""
    try:
        return fn()
    except WorkflowError:
        return default


def _serialize(order: Order, svc: OrderService) -> dict:
    """订单详情统一序列化：含明细、可触发动作、流转时间线。"""
    return {
        "id": order.id,
        "order_no": order.order_no,
        "user_id": order.user_id,
        "total_amount": float(order.total_amount),
        "pay_amount": float(order.pay_amount),
        "status": order.status,
        "current_node_key": order.current_node_key,
        "workflow_instance_id": order.workflow_instance_id,
        "address_snapshot": order.address_snapshot,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "items": [
            {
                "sku_id": i.sku_id,
                "sku_name": i.sku_name,
                "spec": i.spec,
                "price": float(i.price),
                "quantity": i.quantity,
                "subtotal": float(i.subtotal),
            }
            for i in order.items
        ],
        # 订单可能尚未绑定流程实例或实例已丢失，此时不应让整个列表接口 500，
        # 而是降级为空列表，保证其余订单仍可正常展示
        "available_events": _safe(lambda: svc.available_events(order), []),
        "timeline": _safe(lambda: svc.get_timeline(order), []),
    }


@router.get("", summary="订单列表")
def list_orders(status: str = "", user_id: int = 0, db: Session = Depends(get_db)):
    stmt = select(Order).options(selectinload(Order.items))
    if status:
        stmt = stmt.where(Order.status == status)
    if user_id:
        stmt = stmt.where(Order.user_id == user_id)
    orders = db.execute(stmt.order_by(Order.id.desc())).scalars().unique().all()
    svc = OrderService(db)
    return [_serialize(o, svc) for o in orders]


@router.get("/{order_id}", summary="订单详情")
def get_order(order_id: int, db: Session = Depends(get_db)):
    order = db.execute(
        select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
    ).scalars().unique().first()
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    return _serialize(order, OrderService(db))


@router.post("", status_code=201, summary="创建订单（自动启动工作流）")
def create_order(payload: OrderCreateIn, db: Session = Depends(get_db)):
    svc = OrderService(db)
    try:
        order = svc.create_order(
            user_id=payload.user_id,
            items=[i.model_dump() for i in payload.items],
            address_id=payload.address_id,
            remark=payload.remark,
        )
    except ValueError as exc:
        # 库存不足、SKU 不存在等属于业务校验失败，用 400 而不是 500
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize(order, svc)


@router.post("/{order_id}/actions/{event}", summary="推进订单流转")
def fire_event(
    order_id: int, event: str, payload: ActionIn = ActionIn(), db: Session = Depends(get_db)
):
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")

    svc = OrderService(db)
    # 先问引擎「当前能不能执行」，给出比引擎报错更友好的提示
    allowed = [e["event"] for e in svc.available_events(order)]
    if event not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"当前节点 `{order.current_node_key}` 不可执行 `{event}`"
            f"（可执行：{allowed or '无'}）",
        )
    try:
        # 事件名直接透传给服务层，新增流程事件无需改动 API 代码
        order = svc.trigger(order.id, event, payload.operator, payload.comment)
    except (WorkflowError, ValueError) as exc:
        # 非法流转、参数不合法属于调用方问题 -> 400
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 其余异常不在此捕获：编程错误应表现为 500 并留下堆栈，
    # 笼统地转成 400 会把 bug 伪装成用户错误，还会泄漏内部异常信息
    return _serialize(order, svc)
