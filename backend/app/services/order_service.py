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
from sqlalchemy.exc import IntegrityError
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
    """支付成功：记录支付流水。

    **必须幂等**。真实渠道路径下，「发起支付」那一步已写好一条 pending 流水，
    渠道回调先把它置成 success、再触发本副作用。若这里只看 pending，
    就会因为「已经被回调置成功了、找不到 pending」而**再建一条**——
    同一笔订单出现多条支付流水，对账时被算成收了多次钱，
    这种错在财务报表上极难发现。

    故三种情况分别处理：
      - 有 pending  → 置成 success（回调直接推进的路径）
      - 已是 success → 不动（重复回调 / 重复推进）
      - 都没有      → 新建（管理员手动推进、mock 渠道这类没有「发起」记录的场景）
    """
    now = datetime.now()
    existing = (
        svc.db.execute(
            select(Payment)
            .where(Payment.order_id == order.id, Payment.status.in_(("pending", "success")))
            .order_by(Payment.id.desc())
        )
        .scalars()
        .first()
    )
    if existing is not None:
        if existing.status == "pending":
            existing.status = "success"
        existing.paid_at = existing.paid_at or now
        order.paid_at = existing.paid_at
        return

    from app.core.payment import get_payment_provider

    payment = Payment(
        order_id=order.id,
        pay_no=_gen_no("PAY"),
        amount=order.pay_amount,
        channel=get_payment_provider().name,
        status="success",
        paid_at=now,
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

    def _find_order_by_idempotency(self, key: str, user_id: int) -> "Order | None":
        """按下单幂等键查已有订单（同用户）。用于下单幂等：命中即返回、不重复创建。

        用 (idempotency_key, user_id) 双条件：即便两个不同用户碰巧生成了相同的 key，
        也绝不会把 A 的订单透给 B —— 但唯一约束在列上（全局），B 的重复插入仍会被
        IntegrityError 拦下，数据不会被串改。
        """
        return (
            self.db.execute(
                select(Order).where(
                    Order.idempotency_key == key, Order.user_id == user_id
                )
            )
            .scalars()
            .first()
        )

    def create_order(
        self,
        user_id: int,
        items: list[dict],
        address_id: int | None = None,
        remark: str = "",
        idempotency_key: str | None = None,
        auto_commit: bool = True,
    ) -> Order:
        """创建订单：校验库存 → 算钱 → 扣库存 → 启动流程 → 推进到待付款。

        idempotency_key：下单幂等键。客户端每次「意图下单」生成一个，重复提交
        （双击 / 网络重试）带同一 key 即可返回已创建的订单而不重复扣库存。
        非空且在库中存在（同用户）→ 直接返回已有订单；并发下若两条都越过预查，
        后插入者会触发唯一约束冲突（IntegrityError），在 flush 处捕获后回滚本次
        库存扣减并返回已存在的订单，保证并发下也只建一单。可为空（旧订单 / 不
        传 key 的调用方向后兼容）。

        auto_commit=False 时只 flush 不提交，供调用方把「下单」与别的写操作
        （如清空购物车）放进同一个事务 —— 否则会出现订单已生成、
        购物车却没清空的中间态。
        """
        if not items:
            raise ValueError("订单不能没有商品")

        # 下单幂等：同一 key（同用户）只建一单。先查已有订单直接返回，
        # 不重复扣库存、不重复启动流程。并发碰撞的兜底见下方 flush 处的 IntegrityError。
        if idempotency_key is not None:
            existing = self._find_order_by_idempotency(idempotency_key, user_id)
            if existing is not None:
                return existing

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
        if idempotency_key is not None:
            order.idempotency_key = idempotency_key
        try:
            self.db.flush()
        except IntegrityError:
            # 并发重复插入：另一请求已先提交同一 key 的订单。
            # 唯一约束冲突发生在 order 这一行 flush 时（库存扣减已在本会话执行、
            # 但还没提交）。auto_commit=True 时回滚本次事务（含上面的原子扣库存），
            # 再查回已存在的订单返回，保证最终只建一单、库存只扣一次。
            # auto_commit=False 由调用方掌管事务，此处不擅自回滚、直接上抛，
            # 交由调用方处理（预查已覆盖绝大多数重复提交，此分支极少见）。
            if auto_commit:
                self.db.rollback()
                # 用直查而非 _find_order_by_idempotency，使捕获逻辑独立于预查助手：
                # 即使预查被绕过（如并发竞态或测试桩），兜底仍能正确找回已存在的订单。
                existing = (
                    self.db.execute(
                        select(Order).where(
                            Order.idempotency_key == idempotency_key,
                            Order.user_id == user_id,
                        )
                    )
                    .scalars()
                    .first()
                )
                if existing is not None:
                    return existing
            raise

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
