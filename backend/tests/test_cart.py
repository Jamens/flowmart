"""购物车 API 测试。

重点验证四类容易出错的行为：
1. 同一 SKU 重复加入应累加而非新增行
2. 累加后仍受库存约束，不能靠反复加入绕过
3. 结算失败时购物车必须保留（事务回滚），不能出现「订单没成、购物车却空了」
4. 不能越权修改/删除他人的购物车项
"""
import pytest

from app.models.ecommerce import Product, Sku


@pytest.fixture
def sku(db):
    p = Product(name="购物车测试商品", status="on_sale")
    db.add(p)
    db.flush()
    s = Sku(product_id=p.id, sku_code="C-001", spec="默认", price=50, stock=5)
    db.add(s)
    db.commit()
    return s


def add(client, sku_id, qty, user_id=1):
    return client.post(
        "/api/v1/cart", json={"user_id": user_id, "sku_id": sku_id, "quantity": qty}
    )


def cart(client, user_id=1):
    return client.get(f"/api/v1/cart?user_id={user_id}").json()


def test_add_and_list(client, sku):
    assert add(client, sku.id, 2).status_code == 201
    data = cart(client)
    assert len(data["items"]) == 1
    assert data["items"][0]["quantity"] == 2
    assert data["items"][0]["subtotal"] == 100
    assert data["total"] == 100


def test_same_sku_accumulates(client, sku):
    add(client, sku.id, 2)
    add(client, sku.id, 3)
    items = cart(client)["items"]
    # 累加成一行，而不是出现两条记录
    assert len(items) == 1
    assert items[0]["quantity"] == 5


def test_add_beyond_stock_rejected(client, sku):
    r = add(client, sku.id, 99)
    assert r.status_code == 400
    assert "库存不足" in r.json()["detail"]


def test_accumulate_beyond_stock_rejected(client, sku):
    """反复加入不能突破库存上限（库存为 5）。"""
    assert add(client, sku.id, 3).status_code == 201
    r = add(client, sku.id, 3)  # 累计 6 > 5
    assert r.status_code == 400
    # 失败时购物车应保持原样，不能被写成脏数据
    assert cart(client)["items"][0]["quantity"] == 3


def test_update_quantity(client, sku):
    add(client, sku.id, 4)
    item_id = cart(client)["items"][0]["id"]
    r = client.patch(f"/api/v1/cart/{item_id}", json={"user_id": 1, "quantity": 1})
    assert r.status_code == 200
    assert cart(client)["items"][0]["quantity"] == 1


def test_update_beyond_stock_rejected(client, sku):
    add(client, sku.id, 1)
    item_id = cart(client)["items"][0]["id"]
    r = client.patch(f"/api/v1/cart/{item_id}", json={"user_id": 1, "quantity": 99})
    assert r.status_code == 400


def test_remove_by_zero_quantity(client, sku):
    add(client, sku.id, 1)
    item_id = cart(client)["items"][0]["id"]
    client.patch(f"/api/v1/cart/{item_id}", json={"user_id": 1, "quantity": 0})
    assert cart(client)["items"] == []


def test_cannot_touch_other_users_cart(client, sku):
    """越权防护：用户 2 不能改也不能删用户 1 的购物车项。"""
    add(client, sku.id, 1, user_id=1)
    item_id = cart(client, user_id=1)["items"][0]["id"]

    assert client.patch(
        f"/api/v1/cart/{item_id}", json={"user_id": 2, "quantity": 99}
    ).status_code == 404
    assert client.delete(f"/api/v1/cart/{item_id}?user_id=2").status_code == 404

    # 原所有者的数据必须完好无损
    assert cart(client, user_id=1)["items"][0]["quantity"] == 1


def test_carts_are_isolated_between_users(client, sku):
    add(client, sku.id, 1, user_id=1)
    add(client, sku.id, 2, user_id=2)
    assert cart(client, user_id=1)["items"][0]["quantity"] == 1
    assert cart(client, user_id=2)["items"][0]["quantity"] == 2


def test_checkout_creates_order_and_clears_cart(client, sku):
    add(client, sku.id, 2)
    r = client.post("/api/v1/cart/checkout", json={"user_id": 1})
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["pay_amount"] == 100
    assert data["status"] == "pending_payment"
    # 结算后购物车必须清空
    assert cart(client)["items"] == []


def test_checkout_partial_items(client, db, sku):
    """只结算指定商品，其余留在购物车。"""
    p2 = Product(name="第二个商品", status="on_sale")
    db.add(p2)
    db.flush()
    s2 = Sku(product_id=p2.id, sku_code="C-002", spec="规格B", price=20, stock=9)
    db.add(s2)
    db.commit()

    add(client, sku.id, 1)  # 50
    add(client, s2.id, 1)  # 20
    items = cart(client)["items"]
    target = [i["id"] for i in items if i["sku_id"] == s2.id]

    r = client.post("/api/v1/cart/checkout", json={"user_id": 1, "item_ids": target})
    assert r.status_code == 201
    assert r.json()["pay_amount"] == 20  # 只结算了第二个商品

    remain = cart(client)["items"]
    assert len(remain) == 1
    assert remain[0]["sku_id"] == sku.id


def test_checkout_rejects_unknown_item_ids(client, sku):
    """传入不存在的 id 必须报错，不能静默只结算命中的部分。"""
    add(client, sku.id, 1)
    r = client.post("/api/v1/cart/checkout", json={"user_id": 1, "item_ids": [9999]})
    assert r.status_code == 400
    # 购物车不应被清空
    assert len(cart(client)["items"]) == 1


def test_checkout_failure_keeps_cart(client, db, sku):
    """结算失败时购物车不能被清空 —— 验证事务回滚确实生效。"""
    add(client, sku.id, 5)  # 刚好等于库存
    # 模拟并发抢购：库存被别人买走了
    sku.stock = 1
    db.commit()

    r = client.post("/api/v1/cart/checkout", json={"user_id": 1})
    assert r.status_code == 400
    # 关键断言：购物车内容必须还在
    items = cart(client)["items"]
    assert len(items) == 1
    assert items[0]["quantity"] == 5


def test_checkout_empty_cart(client):
    r = client.post("/api/v1/cart/checkout", json={"user_id": 99})
    assert r.status_code == 400
    assert "购物车为空" in r.json()["detail"]
