"""下单幂等：idempotency_key 保证重复提交（双击 / 网络重试）只建一单、只扣一次库存。

覆盖：
- 同一 key 重复提交 → 返回已存在的订单，不重复扣库存、不重复启动流程；
- 不传 key / 不同 key → 各自新建，互不干扰；
- 并发竞态（预查同时越过、后插入者触发唯一约束冲突）→ 捕获 IntegrityError、
  回滚本次库存扣减、返回已存在的订单，最终只建一单；
- 经 API 入口同样幂等。
"""
import pytest
from sqlalchemy import select

from app.models.ecommerce import Order, Product, Sku
from app.services.order_service import OrderService


@pytest.fixture
def sku(db):
    p = Product(name="幂等测试商品", status="on_sale")
    db.add(p)
    db.flush()
    s = Sku(product_id=p.id, sku_code="IDEM-SKU", spec="默认", price=100, stock=10)
    db.add(s)
    db.commit()
    return s


def _create(db, sku, key=None, user_id=1, quantity=1):
    return OrderService(db).create_order(
        user_id=user_id,
        items=[{"sku_id": sku.id, "quantity": quantity}],
        idempotency_key=key,
        auto_commit=True,
    )


def test_same_key_returns_existing_and_skips_stock(db, sku, order_flow):
    first = _create(db, sku, key="KEY-1")
    assert db.get(Sku, sku.id).stock == 9  # 首单扣了 1

    second = _create(db, sku, key="KEY-1")
    # 返回的是同一张订单，没有新建、没有重复扣库存
    assert second.id == first.id
    assert second.order_no == first.order_no
    assert db.get(Sku, sku.id).stock == 9  # 库存没被第二次扣
    # 全库只有一张订单（没有重复创建）
    assert len(db.execute(select(Order)).scalars().all()) == 1


def test_different_or_missing_key_creates_distinct(db, sku, order_flow):
    a = _create(db, sku, key=None)
    b = _create(db, sku, key="KEY-B")
    c = _create(db, sku, key=None)  # 两次不传 key 各建各的

    assert len({a.id, b.id, c.id}) == 3
    assert db.get(Sku, sku.id).stock == 7  # 三次各扣 1
    assert len(db.execute(select(Order)).scalars().all()) == 3


def test_concurrent_integrity_error_returns_existing(db, sku, order_flow):
    """模拟并发竞态：两条请求同时越过预查，后插入者触发唯一约束冲突。

    通过把预查助手桩成「找不到」来复现「预查未命中但插入会撞键」的窗口，
    验证 flush 处的 IntegrityError 兜底能回滚本次扣库存并返回已存在的订单。
    """
    # 先正常建一张带 key 的订单并落库
    first = _create(db, sku, key="KEY-1")
    assert db.get(Sku, sku.id).stock == 9

    svc = OrderService(db)
    # 桩掉预查：让代码误以为没有已有订单，从而真的走到插入 + 撞唯一约束的路径
    svc._find_order_by_idempotency = lambda key, user_id: None  # noqa: ARG005

    # 该调用会扣一次库存（临时减到 8），随后 flush 撞唯一约束被捕获、回滚
    replay = svc.create_order(
        user_id=1,
        items=[{"sku_id": sku.id, "quantity": 1}],
        idempotency_key="KEY-1",
        auto_commit=True,
    )

    assert replay.id == first.id  # 返回的是已存在的订单
    assert db.get(Sku, sku.id).stock == 9  # 回滚后库存仍只被首单扣了 1
    assert len(db.execute(select(Order)).scalars().all()) == 1


def test_idempotency_via_api(client, db, sku, order_flow):
    body = {"items": [{"sku_id": sku.id, "quantity": 1}], "idempotency_key": "API-KEY-1"}
    r1 = client.post("/api/v1/orders", json=body)
    assert r1.status_code == 201
    no1 = r1.json()["order_no"]
    assert r1.json()["idempotency_key"] == "API-KEY-1"

    # 同 key 重放：应返回同一订单，不重复扣库存
    r2 = client.post("/api/v1/orders", json=body)
    assert r2.status_code == 201
    assert r2.json()["order_no"] == no1
    # 库存只被扣一次（起始 10 → 9）
    assert db.get(Sku, sku.id).stock == 9
