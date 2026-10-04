"""expire_unpaid_orders：超时未支付订单自动取消并归还库存。

覆盖：超时单被取消 + 库存归还 + 未支付流水过期；未超时单不动；幂等（跑两次不重复归还）；
超时未启用（timeout<=0）跳过；dry-run 不改任何状态。
"""
from datetime import timedelta

import pytest
from scripts.expire_unpaid_orders import expire_unpaid_orders
from sqlalchemy import select

from app.core.security import utcnow_naive
from app.models.ecommerce import Order, Payment, Product, Sku
from app.services.order_service import OrderService


@pytest.fixture
def sku(db):
    p = Product(name="超时回收测试商品", status="on_sale")
    db.add(p)
    db.flush()
    s = Sku(product_id=p.id, sku_code="EXPIRE-SKU", spec="默认", price=100, stock=10)
    db.add(s)
    db.commit()
    return s


def _place_order(db, sku, when=None, quantity=1):
    """下一张待付款订单，可选把 created_at 回拨到 when（模拟过去的下单时间）。"""
    svc = OrderService(db)
    order = svc.create_order(
        user_id=1,
        items=[{"sku_id": sku.id, "quantity": quantity}],
        auto_commit=True,
    )
    if when is not None:
        order.created_at = when
        db.commit()
    return order


def test_expired_order_cancelled_and_stock_returned(db, sku, order_flow):
    now = utcnow_naive()
    order = _place_order(db, sku, when=now - timedelta(minutes=60))
    assert db.get(Sku, sku.id).stock == 9  # 下单扣了 1
    # 模拟买家已发起支付但迟迟未完成的场景：存在一条待支付流水
    db.add(
        Payment(
            order_id=order.id,
            pay_no="PAYTEST1",
            amount=order.pay_amount,
            channel="mock",
            status="pending",
        )
    )
    db.commit()

    result = expire_unpaid_orders(db, now=now, timeout_minutes=30)

    assert result["expired"] == 1
    assert db.get(Order, order.id).current_node_key == "closed"
    assert db.get(Sku, sku.id).stock == 10  # 库存已归还
    payments = db.execute(select(Payment).where(Payment.order_id == order.id)).scalars().all()
    assert [p.status for p in payments] == ["expired"]


def test_recent_order_untouched(db, sku, order_flow):
    now = utcnow_naive()
    order = _place_order(db, sku, when=now - timedelta(minutes=5))
    result = expire_unpaid_orders(db, now=now, timeout_minutes=30)

    assert result["expired"] == 0
    assert db.get(Order, order.id).current_node_key == "pending_payment"
    assert db.get(Sku, sku.id).stock == 9  # 库存没动


def test_idempotent_no_double_return(db, sku, order_flow):
    now = utcnow_naive()
    _place_order(db, sku, when=now - timedelta(minutes=60))
    expire_unpaid_orders(db, now=now, timeout_minutes=30)
    expire_unpaid_orders(db, now=now, timeout_minutes=30)  # 第二次幂等

    # 库存没有因为第二次跑而变成 11（没有重复归还）
    assert db.get(Sku, sku.id).stock == 10


def test_disabled_when_timeout_le_zero(db, sku, order_flow):
    now = utcnow_naive()
    order = _place_order(db, sku, when=now - timedelta(minutes=60))
    result = expire_unpaid_orders(db, now=now, timeout_minutes=0)

    assert result["disabled"] is True
    assert result["expired"] == 0
    assert db.get(Order, order.id).current_node_key == "pending_payment"


def test_dry_run_does_not_change(db, sku, order_flow):
    now = utcnow_naive()
    order = _place_order(db, sku, when=now - timedelta(minutes=60))
    result = expire_unpaid_orders(db, now=now, timeout_minutes=30, dry_run=True)

    assert result["expired"] == 0
    assert result["would_expire"] == 1
    assert db.get(Order, order.id).current_node_key == "pending_payment"
    assert db.get(Sku, sku.id).stock == 9
