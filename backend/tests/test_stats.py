"""统计看板。

重点不是「返回一堆数字」，而是**口径正确**：
- GMV 只算**已支付**订单，未支付的不计入销售额
- 低库存阈值可配（不同品类「缺货」标准不同，写死没意义）
- 只做 SQL 聚合，不把明细读进内存
- 经营数据仅管理员可见
"""
from decimal import Decimal

from app.models.ecommerce import Product, Sku


def _mk_sku(db, name: str, status: str, sku_code: str, price: str, stock: int) -> Sku:
    p = Product(name=name, status=status)
    db.add(p)
    db.flush()
    s = Sku(product_id=p.id, sku_code=sku_code, spec="默认",
            price=Decimal(price), stock=stock)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _order(client, sku_id: int, quantity: int = 1):
    return client.post(
        "/api/v1/orders", json={"items": [{"sku_id": sku_id, "quantity": quantity}]}
    )


# ---------------- 权限 ----------------


def test_admin_can_read_overview(client):
    r = client.get("/api/v1/stats")
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"orders", "products", "users", "recent"} <= set(body)


def test_buyer_cannot_read_stats(buyer_client):
    assert buyer_client.get("/api/v1/stats").status_code == 403


def test_anonymous_cannot_read_stats(raw_client):
    assert raw_client.get("/api/v1/stats").status_code == 401


def test_buyer_cannot_read_trend(buyer_client):
    assert buyer_client.get("/api/v1/stats/trend").status_code == 403


# ---------------- 口径 ----------------


def test_counts_are_correct(client, db):
    hot = _mk_sku(db, "热销品", "on_sale", "S-HOT", "10.00", 3)
    _mk_sku(db, "下架品", "off_shelf", "S-COLD", "99.00", 100)

    r = _order(client, hot.id, quantity=2)
    assert r.status_code == 201, r.text
    oid = r.json()["id"]
    assert client.post(f"/api/v1/orders/{oid}/actions/pay").status_code == 200

    body = client.get("/api/v1/stats?low_stock_threshold=5").json()
    assert body["orders"]["total"] == 1
    assert body["orders"]["paid_amount"] == 20.0
    assert body["products"]["total"] == 2
    assert body["products"]["on_sale"] == 1
    assert body["products"]["off_shelf"] == 1
    assert body["products"]["sku_total"] == 2
    assert body["products"]["stock_total"] == 1 + 100, "下单扣减后应反映真实库存"
    assert body["products"]["low_stock"] == 1, "只剩 1 件，应判低库存"
    assert body["recent"]["orders"] == 1


def test_gmv_excludes_unpaid_orders(client, db):
    """未支付订单计入订单数，但不计入销售额——这是最容易搞错的口径。"""
    sku = _mk_sku(db, "待付款品", "on_sale", "S-GMV", "50.00", 10)
    assert _order(client, sku.id).status_code == 201

    body = client.get("/api/v1/stats").json()
    assert body["orders"]["total"] == 1
    assert body["orders"]["paid_amount"] == 0.0


def test_low_stock_threshold_is_configurable(client, db):
    _mk_sku(db, "品", "on_sale", "S-LS", "1.00", 8)
    assert client.get("/api/v1/stats?low_stock_threshold=5").json()["products"]["low_stock"] == 0
    assert client.get("/api/v1/stats?low_stock_threshold=10").json()["products"]["low_stock"] == 1


def test_status_breakdown(client, db):
    sku = _mk_sku(db, "品", "on_sale", "S-ST", "10.00", 10)
    r = _order(client, sku.id)
    oid = r.json()["id"]

    pending = client.get("/api/v1/stats").json()["orders"]["by_status"]
    assert pending.get("pending_payment") == 1

    client.post(f"/api/v1/orders/{oid}/actions/pay")
    paid = client.get("/api/v1/stats").json()["orders"]["by_status"]
    assert paid.get("paid") == 1


def test_active_users_counted(client, db):
    body = client.get("/api/v1/stats").json()
    assert body["users"]["active_total"] >= 1, "管理员本人应计入"


# ---------------- 趋势 ----------------


def test_trend_returns_daily_bucket(client, db):
    sku = _mk_sku(db, "品", "on_sale", "S-T", "5.00", 10)
    r = _order(client, sku.id)
    assert r.status_code == 201, r.text
    oid = r.json()["id"]
    assert client.post(f"/api/v1/orders/{oid}/actions/pay").status_code == 200

    body = client.get("/api/v1/stats/trend?days=7").json()
    assert body["days"] == 7
    assert len(body["items"]) == 1, "今天应有且仅有一个桶"
    assert body["items"][0]["orders"] == 1
    assert body["items"][0]["paid_amount"] == 5.0


def test_trend_rejects_out_of_range_days(client):
    assert client.get("/api/v1/stats/trend?days=0").status_code == 422
    assert client.get("/api/v1/stats/trend?days=999").status_code == 422
