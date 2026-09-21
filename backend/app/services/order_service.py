"""订单业务服务：电商流程与工作流引擎的粘合层。

核心约定：**订单状态只允许通过流程引擎变更**，业务代码不直接改 status。

关键设计 —— 副作用（Side Effect）与流转（Transition）解耦：
- 流转由流程定义决定，引擎负责推进
- 副作用（写支付流水、归还库存等）按「事件名」注册到 SIDE_EFFECTS
- 未在注册表里的事件默认「纯推进」，不产生副作用

这样在流程设计器里新增审批、驳回等事件时，后端代码一行都不用改。
"""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ecommerce import (
    Address,
    Order,
    OrderItem,
    Payment,
    Sku,
)
from app.models.workflow import WorkflowInstance
from app.services.workflow_engine import WorkflowEngine, WorkflowError

ORDER_FLOW_CODE = "order_flow"


def _gen_no(prefix: str) -> str:
    """生成业务单号：时间戳前缀 + 随机后缀，可读且不重复。"""
    return f"{prefix}{datetime.now().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:6].upper()}"


def sync_order_status(order: Order, instance: WorkflowInstance) -> None:
    """把流程实例的当前节点同步回订单。

    这是唯一允许写 order.status 的地方，避免状态与流程脱节。
    """
    order.current_node_key = instance.current_node_key
    order.status = instance.current_node_key
    if instance.status == "finished":
        order.finished_at = instance.finished_at or datetime.now()


# ---------------- 副作用：与社会性无关的数据库操作 ----------------

def _effect_create_payment(svc: "OrderService", order: Order) -> None:
    """支付成功：记录支付流水。"""
    payment = Payment(
        order_id=order.id,
        pay_no=_gen_no("PAY"),
        amount=order.pay_amount,
        channel="mock",
        status="success",
        paid_at=datetime.now(),
    )
    svc.db.add(payment)
    order.paid_at = payment.paid_at


def _effect_return_stock(svc: "OrderService", order: Order) -> None:
    """归还库存：取消与退款都必须调用，否则库存会凭空消失。"""
    for item in order.items:
        sku = svc.db.get(Sku, item.sku_id)
        if sku:
            sku.stock += item.quantity


def _effect_mark_refunded(svc: "OrderService", order: Order) -> None:
    """退款：在原支付流水上标记已退款。"""
    _effect_return_stock(svc, order)
    payment = (
        svc.db.execute(
            select(Payment).where(
                Payment.order_id == order.id, Payment.status == "success"
            )
        )
        .scalars()
        .first()
    )
    if payment:
        payment.status = "refunded"


def _effect_mark_shipped(svc: "OrderService", order: Order) -> None:
    order.shipped_at = datetime.now()


# 事件名 -> 副作用。设计器里新增事件后，按需在此登记副作用即可；
# 未登记的事件仍能正常流转，只是不产生额外数据变更。
SIDE_EFFECTS = {
    "pay": _effect_create_payment,
    "ship": _effect_mark_shipped,
    "cancel": _effect_return_stock,
    "refund": _effect_mark_refunded,
}


class OrderService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.engine = WorkflowEngine(db)

    # ---------- 下单 ----------

    def create_order(
        self,
        user_id: int,
        items: list[dict],
        address_id: int | None = None,
        remark: str = "",
        auto_commit: bool = True,
    ) -> Order:
        """创建订单：校验库存 → 算钱 → 扣库存 → 启动流程 → 推进到待付款。

        auto_commit=False 时只 flush 不提交，供调用方把「下单」与别的写操作
        （如清空购物车）放进同一个事务 —— 否则会出现订单已生成、
        购物车却没清空的中间态。
        """
        if not items:
            raise ValueError("订单不能没有商品")

        address = None
        if address_id:
            address = self.db.get(Address, address_id)
            if address is None:
                raise ValueError(f"收货地址 {address_id} 不存在")

        total = Decimal("0.00")
        order_items: list[OrderItem] = []

        for raw in items:
            sku_id = int(raw["sku_id"])
            quantity = int(raw.get("quantity", 1))
            if quantity <= 0:
                raise ValueError("商品数量必须大于 0")

            sku = self.db.get(Sku, sku_id)
            if sku is None:
                raise ValueError(f"SKU {sku_id} 不存在")
            if sku.status != "on_sale":
                raise ValueError(f"SKU {sku_id} 已下架")
            if sku.stock < quantity:
                raise ValueError(
                    f"SKU {sku_id} 库存不足（剩 {sku.stock}，需要 {quantity}）"
                )

            subtotal = Decimal(str(sku.price)) * quantity
            total += subtotal

            # 价格与名称做快照：商品日后改价或下架，历史订单不受影响
            order_items.append(
                OrderItem(
                    sku_id=sku.id,
                    sku_name=sku.product.name if sku.product else sku.sku_code,
                    spec=sku.spec,
                    price=sku.price,
                    quantity=quantity,
                    subtotal=subtotal,
                )
            )
            sku.stock -= quantity

        snapshot = ""
        if address:
            snapshot = (
                f"{address.receiver} {address.phone} "
                f"{address.province}{address.city}{address.district}{address.detail}"
            )

        order = Order(
            order_no=_gen_no("NO"),
            user_id=user_id,
            total_amount=total,
            pay_amount=total,
            address_snapshot=snapshot,
            remark=remark,
        )
        order.items = order_items
        self.db.add(order)
        self.db.flush()

        instance = self.engine.start(
            ORDER_FLOW_CODE,
            biz_type="order",
            biz_id=str(order.id),
            context={
                "amount": float(total),
                "user_id": user_id,
                "order_no": order.order_no,
            },
            operator=f"user:{user_id}",
        )
        self.engine.fire(
            instance.id, "submit", operator=f"user:{user_id}", comment="用户下单"
        )

        order.workflow_instance_id = instance.id
        sync_order_status(order, instance)
        if auto_commit:
            self.db.commit()
        return order

    # ---------- 流转 ----------

    def _load_instance(self, order: Order) -> WorkflowInstance:
        if not order.workflow_instance_id:
            raise WorkflowError(f"订单 {order.order_no} 未绑定流程实例")
        instance = self.db.get(WorkflowInstance, order.workflow_instance_id)
        if instance is None:
            raise WorkflowError(f"订单 {order.order_no} 的流程实例不存在")
        return instance

    def trigger(
        self,
        order_id: int,
        event: str,
        operator: str = "system",
        comment: str = "",
    ) -> Order:
        """通用流转入口：执行该事件注册的副作用，然后推进流程。

        API 层只需把前端传来的事件名透传进来，无需为每种事件写分支。
        """
        order = self.db.get(Order, order_id)
        if order is None:
            raise ValueError(f"订单 {order_id} 不存在")

        effect = SIDE_EFFECTS.get(event)
        if effect:
            effect(self, order)

        instance = self._load_instance(order)
        self.engine.fire(
            instance.id, event, operator=operator, comment=comment
        )
        sync_order_status(order, instance)
        self.db.commit()
        return order

    # 以下为语义化便捷方法，种子脚本与测试用；实质都走 trigger
    def pay(self, order_id: int) -> Order:
        return self.trigger(order_id, "pay", comment="支付成功")

    def ship(self, order_id: int, operator: str = "admin") -> Order:
        return self.trigger(order_id, "ship", operator=operator, comment="商家发货")

    def confirm(self, order_id: int, operator: str = "user") -> Order:
        return self.trigger(order_id, "confirm", operator=operator, comment="确认收货")

    def cancel(self, order_id: int, operator: str = "user") -> Order:
        return self.trigger(order_id, "cancel", operator=operator, comment="取消订单")

    def refund(self, order_id: int, operator: str = "admin") -> Order:
        return self.trigger(order_id, "refund", operator=operator, comment="退款")

    def available_events(self, order: Order) -> list[dict]:
        """当前订单可执行的动作，供前端渲染按钮。"""
        instance = self._load_instance(order)
        return self.engine.available_events(instance.id)

    def get_timeline(self, order: Order) -> list[dict]:
        """订单流转时间线。"""
        from app.models.workflow import WorkflowTransitionLog

        instance = self._load_instance(order)
        logs = (
            self.db.execute(
                select(WorkflowTransitionLog)
                .where(WorkflowTransitionLog.instance_id == instance.id)
                .order_by(WorkflowTransitionLog.id)
            )
            .scalars()
            .all()
        )
        return [
            {
                "from": log.from_node_key,
                "to": log.to_node_key,
                "event": log.event,
                "operator": log.operator,
                "comment": log.comment,
                "created_at": log.created_at.isoformat() if log.created_at else None,
            }
            for log in logs
        ]
