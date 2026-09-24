"""工作流推进的并发安全：防止重复触发让副作用（归还库存）执行两次。

背景：fire() 原先是「读到当前节点 → 裸赋值 → flush」，两个并发请求会各自读到
同一个节点、都判定可流转、都执行副作用 —— cancel/refund 的归还库存就会跑两次，
库存凭空变多。买家自助动作开放后，这个面从管理员扩大到了所有买家。

修复两点：
1. fire() 改用「条件 UPDATE + rowcount」认领推进，后到的请求必然匹配不到行而显式失败；
2. trigger() 把副作用挪到 fire 成功之后，失败路径上副作用根本没发生过（不依赖回滚兜底）。
"""
import pytest
from sqlalchemy import update

from app.models.ecommerce import Product, Sku
from app.models.workflow import WorkflowInstance
from app.services.workflow_engine import WorkflowError


@pytest.fixture
def sku(db):
    """一个可用于下单的 SKU（库存 10）。"""
    p = Product(name="并发测试商品", status="on_sale")
    db.add(p)
    db.flush()
    s = Sku(product_id=p.id, sku_code="CONC-SKU", spec="默认", price=100, stock=10)
    db.add(s)
    db.commit()
    return s


def _new_order(client, sku, quantity=1):
    r = client.post("/api/v1/orders",
                    json={"items": [{"sku_id": sku.id, "quantity": quantity}]})
    assert r.status_code == 201, r.text
    return r.json()


def test_fire_rejects_when_node_was_advanced_concurrently(engine, db, order_flow):
    """核心回归：并发下后到的请求必须失败，而不是覆盖前者。

    模拟方式：ORM 对象仍停留在 pending_payment（identity map 缓存），
    但 DB 里的行已被另一个请求推进到 paid。此时 fire 必须认领失败。

    修复前这里是裸赋值，会「成功」推进并让归还库存执行两次——库存凭空变多。
    """
    inst = engine.start("order_flow", biz_type="order", biz_id="1", context={"amount": 500})
    engine.fire(inst.id, "submit")
    assert inst.current_node_key == "pending_payment"

    # 模拟另一个请求抢先推进：直接改 DB 行，且**不让会话同步 identity map**。
    # SQLAlchemy 2.0 下 session.execute() 执行 ORM UPDATE 默认会同步内存对象，
    # 那样 inst 会立刻变成 paid，测的就不是「ORM 陈旧、DB 已变」这个真实并发场景了。
    db.execute(
        update(WorkflowInstance)
        .where(WorkflowInstance.id == inst.id)
        .values(current_node_key="paid"),
        execution_options={"synchronize_session": False},
    )
    db.flush()
    assert inst.current_node_key == "pending_payment", "ORM 对象应仍处于陈旧的 pending_payment"

    with pytest.raises(WorkflowError) as exc:
        engine.fire(inst.id, "cancel")
    assert "已被并发操作推进" in str(exc.value)

    # DB 里的节点应仍是抢先者写入的 paid，而不是被后到者覆盖成 closed
    db.refresh(inst)
    assert inst.current_node_key == "paid"


def test_available_events_empty_for_finished_instance(engine, order_flow):
    """已结束实例没有任何可执行事件，避免前端渲染出「点了必然报错」的按钮。"""
    inst = engine.start("order_flow", biz_type="order", biz_id="2", context={"amount": 500})
    engine.fire(inst.id, "submit")
    engine.fire(inst.id, "cancel")  # -> closed（end 节点），实例收敛为 finished

    assert inst.status == "finished"
    assert engine.available_events(inst.id) == []


def test_second_cancel_fails_and_stock_returned_once(client, sku, db):
    """**顺序**重复取消：第二次必须失败，库存只归还一次。

    注意这是顺序场景——第二次会在 available_events 处短路、根本走不到 fire()，
    因此对修复前的代码同样通过，**测不出**并发缺陷。
    真正的并发回归见 test_inventory_concurrency.py
    ::test_concurrent_cancel_same_order_returns_stock_once（双线程抢同一订单）。
    """
    order = _new_order(client, sku)

    db.refresh(sku)
    assert sku.stock == 9, "下单应先扣减库存"

    assert client.post(f"/api/v1/orders/{order['id']}/actions/cancel").status_code == 200
    db.refresh(sku)
    assert sku.stock == 10, "取消应归还库存"

    r = client.post(f"/api/v1/orders/{order['id']}/actions/cancel")
    assert r.status_code == 400, "已结束的订单不能再取消"

    db.refresh(sku)
    assert sku.stock == 10, "重复取消不能再归还一次库存"
