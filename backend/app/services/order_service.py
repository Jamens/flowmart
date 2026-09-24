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

from sqlalchemy import select, update
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
    """归还库存：取消与退款都必须调用，否则库存会凭空消失。

    用原子 UPDATE ... SET stock = stock + qty 完成，避免「先读到内存再加回去」的
    丢失更新（lost update）：若两笔归还并发发生，读-改-写会出现一笔加成的库存被
    另一笔覆盖。DB 层直接累加则天然串行、无丢失。
    """
    for item in order.items:
        svc.db.execute(
            update(Sku)
            .where(Sku.id == item.sku_id)
            .values(stock=Sku.stock + item.quantity)
        )


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
            # 越权防护：地址必须归属当前下单用户，否则与「不存在」同等处理，
            # 不泄露他人地址是否存在（与 api/users.py 的 _assert_owner 约定一致）
            if address is None or address.user_id != user_id:
                raise ValueError("收货地址不存在")

        total = Decimal("0.00")
        order_items: list[OrderItem] = []

        for raw in items:
            sku_id = int(raw["sku_id"])
            quantity = int(raw.get("quantity", 1))
            if quantity <= 0:
                raise ValueError("商品数量必须大于 0")

            sku = self.db.get(Sku, sku_id)
            if sku is None:
                if auto_commit:
                    self.db.rollback()
                raise ValueError(f"SKU {sku_id} 不存在")
            if sku.status != "on_sale":
                if auto_commit:
                    self.db.rollback()
                raise ValueError(f"SKU {sku_id} 已下架")
            # 友好预检：非原子，仅用于提前拦截并给出中文库存不足提示。
            # 真正的扣减见下方原子 UPDATE —— 只有它才能杜绝并发超卖。
            if sku.stock < quantity:
                if auto_commit:
                    self.db.rollback()
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

            # 原子扣减：在数据库层用 UPDATE ... WHERE stock >= quantity 完成，
            # 用 rowcount 判断是否真的扣成功。这样即使两个并发请求都读到旧库存、
            # 都通过上面的预检，也只有一行 UPDATE 能拿到 rowcount==1，
            # 另一个 rowcount==0 即判定库存不足 —— 彻底消除「读-改-写」TOCTOU 超卖。
            result = self.db.execute(
                update(Sku)
                .where(Sku.id == sku_id, Sku.stock >= quantity)
                .values(stock=Sku.stock - quantity)
            )
            if result.rowcount == 0:
                # 扣减失败（库存真不够或被并发抢光）：回滚本次事务，
                # 撤销前面 SKU 已执行的原子扣减，避免留下半截状态。
                # auto_commit=True：get_db 只 close 不 rollback，必须自己回滚；
                # auto_commit=False：事务由调用方掌管，这里不动，交给调用方回滚。
                if auto_commit:
                    self.db.rollback()
                real_stock = self.db.execute(
                    select(Sku.stock).where(Sku.id == sku_id)
                ).scalar()
                raise ValueError(
                    f"SKU {sku_id} 库存不足（剩 {real_stock}，需要 {quantity}）"
                )
            # 扣减成功：让同会话后续读取（含同一 SKU 重复出现于多行时的下一行预检）
            # 看到最新库存，避免被过期的身份映射库存误导文案。不影响正确性，
            # 因为下一行是否放行仍由原子 UPDATE 的 rowcount 真正把关。
            self.db.expire(sku)

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
        """通用流转入口：推进流程，**成功之后**才执行该事件注册的副作用。

        顺序刻意是「先推进、后副作用」：fire() 可能因并发认领失败而抛错，
        若先跑副作用，「归还库存」这类操作已经生效，只能依赖异常后的回滚兜底；
        反过来则失败路径上副作用根本没发生过，不依赖回滚这个假设。

        API 层只需把前端传来的事件名透传进来，无需为每种事件写分支。
        """
        order = self.db.get(Order, order_id)
        if order is None:
            raise ValueError(f"订单 {order_id} 不存在")

        instance = self._load_instance(order)
        self.engine.fire(
            instance.id, event, operator=operator, comment=comment
        )

        # 副作用放在 fire 成功之后：fire 可能因并发认领失败而抛错，
        # 若先跑副作用，「归还库存」这类操作已经生效，只能依赖异常后的回滚兜底。
        # 先推进、后跑副作用，失败路径上副作用根本没发生过，不依赖回滚。
        effect = SIDE_EFFECTS.get(event)
        if effect:
            effect(self, order)

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
