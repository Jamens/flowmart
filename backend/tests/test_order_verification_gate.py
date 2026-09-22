"""下单验证闸门测试。

强约束：未验证邮箱/手机的用户禁止创建订单（403）；至少验证其一即可（201）。
注意：conftest 的 client/buyer_client 默认用户已被置为「已验证」，因此本文件的未验证分支
需要显式切到一个未验证用户（client.as_user）来覆盖；这是为了不污染其他下单用例的默认假设。

闸门只作用于 API 层（create_order），服务层 OrderService.create_order 不受限——后台运营/迁移
等直接调用路径不应被账户合规约束拦截。
"""
from app.core.security import hash_password
from app.models.ecommerce import Product, Sku, User


def _make_sku(db):
    p = Product(name="闸门商品", status="on_sale")
    db.add(p)
    db.flush()
    s = Sku(product_id=p.id, sku_code="GATE-SKU", price=50, stock=10)
    db.add(s)
    db.commit()
    return s


def _make_user(db, username, **kw):
    u = User(
        username=username,
        nickname=username,
        phone=kw.get("phone", "13800000999"),
        password_hash=hash_password("123456"),
        is_admin=False,
        email_verified=kw.get("email_verified", False),
        phone_verified=kw.get("phone_verified", False),
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_unverified_user_rejected(client, db, order_flow):
    """未验证（邮箱、手机均未验证）用户下单必须被 403 拦截。"""
    u = _make_user(db, "unverified_buyer")
    client.as_user(u)

    sku = _make_sku(db)
    r = client.post("/api/v1/orders", json={"items": [{"sku_id": sku.id, "quantity": 1}]})
    assert r.status_code == 403, r.text
    assert "验证" in r.json()["detail"]


def test_verified_via_email_can_order(client, db, order_flow):
    """仅验证邮箱即可下单。"""
    u = _make_user(db, "verified_email", email_verified=True)
    client.as_user(u)

    sku = _make_sku(db)
    r = client.post("/api/v1/orders", json={"items": [{"sku_id": sku.id, "quantity": 1}]})
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "pending_payment"


def test_verified_via_phone_only_can_order(client, db, order_flow):
    """仅验证手机（email 仍为空）也可下单——闸门接受任一种联系方式。"""
    u = _make_user(db, "verified_phone", phone="13800000998", phone_verified=True)
    client.as_user(u)

    sku = _make_sku(db)
    r = client.post("/api/v1/orders", json={"items": [{"sku_id": sku.id, "quantity": 1}]})
    assert r.status_code == 201, r.text


def test_revoked_verification_blocks_again(client, db, order_flow):
    """已验证用户若验证状态被撤销（如风控下线），应重新被闸门拦截——验证的是运行时实时状态。"""
    u = _make_user(db, "revoked_user", email_verified=True)
    client.as_user(u)
    sku = _make_sku(db)

    r1 = client.post("/api/v1/orders", json={"items": [{"sku_id": sku.id, "quantity": 1}]})
    assert r1.status_code == 201, r1.text

    u.email_verified = False
    db.commit()
    r2 = client.post("/api/v1/orders", json={"items": [{"sku_id": sku.id, "quantity": 1}]})
    assert r2.status_code == 403, r2.text


def test_cart_checkout_requires_verification(client, db, order_flow):
    """购物车结算是另一条下单入口，必须与直接下单共用同一道验证闸门，否则可被绕过。"""
    u = _make_user(db, "cart_unverified")
    client.as_user(u)
    sku = _make_sku(db)

    # 加购本身不要求验证
    r_add = client.post("/api/v1/cart", json={"sku_id": sku.id, "quantity": 1})
    assert r_add.status_code == 201, r_add.text

    # 未验证结算 → 403，且购物车不被清空
    r_co = client.post("/api/v1/cart/checkout", json={})
    assert r_co.status_code == 403, r_co.text
    assert client.get("/api/v1/cart").json()["items"]  # 购物车仍在

    # 验证后即可结算并清空购物车
    u.email_verified = True
    db.commit()
    r_co2 = client.post("/api/v1/cart/checkout", json={})
    assert r_co2.status_code == 201, r_co2.text
    assert client.get("/api/v1/cart").json()["items"] == []
