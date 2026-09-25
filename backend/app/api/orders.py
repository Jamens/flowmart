"""订单 API。

流转动作统一走 POST /orders/{id}/actions/{event}：
新增流程事件时不需要改 API 代码，由服务层与流程定义决定支持哪些 event。

鉴权：所有接口都必须登录。下单时订单归属固定为当前登录用户（get_current_user），
不再信任请求体里的 user_id —— 这是防冒充下单的关键。订单列表/详情对非管理员
按 user_id 收口为「仅自己的订单」。

推进订单流转（actions）按角色分流，且**两种角色都校验订单归属**：
- 管理员：可对任意订单执行流程定义支持的任意事件（运营操作 ship/refund/approve/reject…）。
- 买家：仅可对自己的订单执行 BUYER_ALLOWED_EVENTS 白名单内的事件（取消订单 / 确认收货）。
  白名单默认是拒绝的——未登记的事件即使流程定义允许，买家也触发不了。
他人订单的越权访问统一 404，不泄露订单是否存在（与订单详情接口一致）。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, or_
from sqlalchemy.orm import Session, selectinload
from datetime import datetime, timedelta

from app.core.database import get_db
from app.core.pagination import apply_pagination, total_count
from app.core.ratelimit import rate_limit_user
from app.core.security import get_current_user, require_verified_contact
from app.models.ecommerce import Order, OrderItem, User
from app.services.order_service import OrderService
from app.services.workflow_engine import WorkflowError

router = APIRouter(prefix="/orders", tags=["订单"])


class OrderItemIn(BaseModel):
    sku_id: int
    quantity: int = Field(1, ge=1)


class OrderCreateIn(BaseModel):
    items: list[OrderItemIn] = Field(..., min_length=1)
    address_id: int | None = None
    remark: str = ""


class ActionIn(BaseModel):
    # operator 刻意不作为入参：一律取认证身份（见 fire_event），
    # 否则调用方可伪造 operator="admin" 把自己的动作记到他人头上、污染流转日志。
    # comment 限长与 wf_transition_logs.comment 列宽（String(255)）对齐：
    # 生产用 MySQL 时超长会 DataError -> 500，而 SQLite 不校验长度、测试跑不出来。
    comment: str = Field("", max_length=255)


# 买家可自助触发的事件白名单。
# 这是**权限策略**，刻意不放进流程定义（wf_transitions）里：让「谁能做什么」集中可见、
# 可审计，而不是散落在流程图节点中难以 review。代价是新增买家可执行事件时要改这里，
# 但权限变更本就该是显式、需要过审的动作。
# 默认拒绝：未登记的事件（ship / refund / approve / reject 等）即使流程定义允许，
# 买家也一律触发不了，必须由管理员执行。
BUYER_ALLOWED_EVENTS = frozenset({"cancel", "confirm"})


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


@router.get("", summary="订单列表（管理员见全部 / 买家仅见自己）")
def list_orders(
    status: str = "",
    # 关键词：匹配订单号或任一商品行项的 SKU 名称（历史订单冗余快照，改价/下架不影响）
    keyword: str = "",
    # 下单时间范围（YYYY-MM-DD，闭区间含当天）。为空表示不限制。
    created_from: str = "",
    created_to: str = "",
    # limit=0 表示不分页（返回全部），保证既有调用方行为不变；le 防超大 limit 拖垮接口
    limit: int = Query(0, ge=0, le=500),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(Order).options(selectinload(Order.items))
    if status:
        stmt = stmt.where(Order.status == status)
    if keyword:
        # 订单号 LIKE 或 任一商品行项名称 LIKE：用 or_ + 关系 any()（生成 EXISTS 子查询），
        # 不 join 主表，因此不会让 total_count 的子查询重复计数
        like = f"%{keyword}%"
        stmt = stmt.where(
            or_(Order.order_no.like(like), Order.items.any(OrderItem.sku_name.like(like)))
        )
    if created_from or created_to:
        # 日期格式错误直接 400，而不是让 DB 抛方言相关的怪错
        try:
            if created_from:
                f = datetime.fromisoformat(created_from)
                stmt = stmt.where(Order.created_at >= f)
            if created_to:
                # 含当天结束：< 次日 0 点，避免漏掉当天的非 0 点订单
                t = datetime.fromisoformat(created_to)
                stmt = stmt.where(Order.created_at < t + timedelta(days=1))
        except ValueError:
            raise HTTPException(status_code=400, detail="created_from/created_to 须为 YYYY-MM-DD")
    # 非管理员只能看自己的订单，避免任意买家遍历全平台订单（PII / 越权）
    if not current_user.is_admin:
        stmt = stmt.where(Order.user_id == current_user.id)
    # 先按同一套过滤条件统计总数，再分页 —— 两处共用同一个 stmt，不会条件漂移
    total = total_count(db, stmt)
    orders = (
        db.execute(apply_pagination(stmt.order_by(Order.id.desc()), limit, offset))
        .scalars()
        .unique()
        .all()
    )
    svc = OrderService(db)
    return {"items": [_serialize(o, svc) for o in orders], "total": total}


@router.get("/{order_id}", summary="订单详情")
def get_order(
    order_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = db.execute(
        select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
    ).scalars().unique().first()
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    # 非管理员只能看自己的订单；他人的订单统一 404，不泄露存在性
    if not current_user.is_admin and order.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="订单不存在")
    return _serialize(order, OrderService(db))


@router.post("", status_code=201, summary="创建订单（自动启动工作流，归属当前用户）")
def create_order(
    payload: OrderCreateIn,
    current_user: User = Depends(require_verified_contact),
    db: Session = Depends(get_db),
    # 下单会原子扣库存，刷单能把库存打到 0（业务型 DoS）。
    # 按 user_id 而非 IP：NAT 下 IP 会误伤一片正常用户，代理下又能随手换
    _rl: None = Depends(rate_limit_user("order")),
):
    svc = OrderService(db)
    try:
        order = svc.create_order(
            user_id=current_user.id,
            items=[i.model_dump() for i in payload.items],
            address_id=payload.address_id,
            remark=payload.remark,
        )
    except ValueError as exc:
        # 库存不足、SKU 不存在等属于业务校验失败，用 400 而不是 500；
        # 地址相关的越权/不存在统一 404（与 _assert_owner 约定一致），不泄露目标是否存在
        status = 404 if "收货地址" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return _serialize(order, svc)


@router.post("/{order_id}/actions/{event}", summary="推进订单流转（管理员任意事件 / 买家仅可取消或确认收货）")
def fire_event(
    order_id: int,
    event: str,
    payload: ActionIn = ActionIn(),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")

    # 权限分流：管理员可对任意订单执行任意事件；买家只能对自己的订单执行白名单事件。
    # 他人的订单统一 404（与订单详情一致，不泄露订单是否存在）。
    if not current_user.is_admin:
        if order.user_id != current_user.id:
            raise HTTPException(status_code=404, detail="订单不存在")
        if event not in BUYER_ALLOWED_EVENTS:
            raise HTTPException(status_code=403, detail=f"买家不可执行 `{event}` 操作")

    svc = OrderService(db)
    # 先问引擎「当前能不能执行」，给出比引擎报错更友好的提示
    allowed = [e["event"] for e in svc.available_events(order)]
    if event not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"当前节点 `{order.current_node_key}` 不可执行 `{event}`"
            f"（可执行：{allowed or '无'}）",
        )
    # operator 一律取自认证身份，不信任请求体：否则买家可在 body 里伪造
    # operator="admin"，把自己的动作记到他人头上、污染 wf_transition_logs 审计轨迹
    operator = current_user.username
    try:
        # 事件名直接透传给服务层，新增流程事件无需改动 API 代码
        order = svc.trigger(order.id, event, operator, payload.comment)
    except (WorkflowError, ValueError) as exc:
        # 非法流转、参数不合法属于调用方问题 -> 400
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 其余异常不在此捕获：编程错误应表现为 500 并留下堆栈，
    # 笼统地转成 400 会把 bug 伪装成用户错误，还会泄漏内部异常信息
    return _serialize(order, svc)
