"""订单业务服务：电商流程与工作流引擎的粘合层。

关键约定：**订单状态只允许通过流程引擎变更**。
业务方法负责算钱、扣库存、写快照，然后调用引擎推进；引擎走到哪个节点，
订单的 status 就同步成哪个节点。这样保证「状态」与「流转日志」永远一致。
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
    """生成业务单号：时间戳前缀 + 随机后缀，保证可读且不重复。"""
    return f"{prefix}{datetime.now().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:6].upper()}"


def sync_order_status(order: Order, instance: WorkflowInstance) -> None:
    """把流程实例的当前节点同步回订单。

    这是唯一允许写 order.status 的地方，避免状态与流程脱节。
    """
    order.current_node_key = instance.current_node_key
    order.status = instance.current_node_key
    if instance.status == "finished":
        order.finished_at = instance.finished_at or datetime.now()


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
    ) -> Order:
        """创建订单：校验库存 → 算钱 → 扣库存 → 启动流程 → 推进到待付款。

        整个操作在同一事务内：库存扣减与流程启动要么都成功，要么都回滚。
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
                raise ValueError(f"SKU {sku_id} 库存不足（剩 {sku.stock}，需要 {quantity}）")

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
            snapshot = f"{address.receiver} {address.phone} {address.province}{address.city}{address.district}{address.detail}"

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

        # 启动流程并把上下文交给引擎，后续条件判断（如退款金额门槛）可用
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
        self.engine.fire(instance.id, "submit", operator=f"user:{user_id}", comment="用户下单")

        order.workflow_instance_id = instance.id
        sync_order_status(order, instance)
        self.db.commit()
        return order

    # ---------- 流转动作 ----------

    def _load_instance(self, order: Order) -> WorkflowInstance:
        if not order.workflow_instance_id:
            raise WorkflowError(f"订单 {order.order_no} 未绑定流程实例")
        instance = self.db.get(WorkflowInstance, order.workflow_instance_id)
        if instance is None:
            raise WorkflowError(f"订单 {order.order_no} 的流程实例不存在")
        return instance

    def _advance(
        self,
        order: Order,
        event: str,
        operator: str = "system",
        comment: str = "",
        runtime_context: dict | None = None,
    ) -> Order:
        """统一的推进入口：触发引擎后同步状态回订单。"""
        instance = self._load_instance(order)
        self.engine.fire(
            instance.id,
            event,
            runtime_context=runtime_context,
            operator=operator,
            comment=comment,
        )
        sync_order_status(order, instance)
        self.db.commit()
        return order

    def pay(self, order_id: int, channel: str = "mock") -> Order:
        """支付：写支付流水，再推进流程到待发货。"""
        order = self.db.get(Order, order_id)
        if order is None:
            raise ValueError(f"订单 {order_id} 不存在")

        payment = Payment(
            order_id=order.id,
            pay_no=_gen_no("PAY"),
            amount=order.pay_amount,
            channel=channel,
            status="success",
            paid_at=datetime.now(),
        )
        self.db.add(payment)
        order.paid_at = payment.paid_at

        return self._advance(order, "pay", operator="system", comment=f"{channel} 支付成功")

    def ship(self, order_id: int, operator: str = "admin") -> Order:
        order = self.db.get(Order, order_id)
        if order is None:
            raise ValueError(f"订单 {order_id} 不存在")
        order.shipped_at = datetime.now()
        return self._advance(order, "ship", operator=operator, comment="商家发货")

    def confirm(self, order_id: int, operator: str = "user") -> Order:
        order = self.db.get(Order, order_id)
        if order is None:
            raise ValueError(f"订单 {order_id} 不存在")
        return self._advance(order, "confirm", operator=operator, comment="确认收货")

    def cancel(self, order_id: int, operator: str = "user") -> Order:
        """取消订单：必须归还库存，否则库存会凭空消失。"""
        order = self.db.get(Order, order_id)
        if order is None:
            raise ValueError(f"订单 {order_id} 不存在")

        for item in order.items:
            sku = self.db.get(Sku, item.sku_id)
            if sku:
                sku.stock += item.quantity

        return self._advance(order, "cancel", operator=operator, comment="取消订单，已归还库存")

    def refund(self, order_id: int, operator: str = "admin") -> Order:
        """退款：金额门槛由流程定义里的条件表达式决定，代码不做硬判断。"""
        order = self.db.get(Order, order_id)
        if order is None:
            raise ValueError(f"订单 {order_id} 不存在")

        for item in order.items:
            sku = self.db.get(Sku, item.sku_id)
            if sku:
                sku.stock += item.quantity

        payment = (
            self.db.execute(
                select(Payment).where(Payment.order_id == order.id, Payment.status == "success")
            )
            .scalars()
            .first()
        )
        if payment:
            payment.status = "refunded"

        return self._advance(order, "refund", operator=operator, comment="退款，已归还库存")

    def available_events(self, order: Order) -> list[dict]:
        """当前订单可执行的动作，供前端渲染按钮。"""
        instance = self._load_instance(order)
        return self.engine.available_events(instance.id)

    def get_timeline(self, order: Order) -> list[dict]:
        """订单流转时间线。"""
        instance = self._load_instance(order)
        from app.models.workflow import WorkflowTransitionLog

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
