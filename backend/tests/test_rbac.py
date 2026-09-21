"""RBAC 角色权限回归测试。

验证「角色闸门」真正生效，而不是只写在文档里：
- 用户管理（建/列/禁用）与订单流转推进仅管理员可执行；
- 普通买家只能操作自己的资料与自己的订单；
- 越权访问他人资源统一 404（不泄露目标是否存在）。

用 raw_client 走真实鉴权链路（不覆盖 get_current_user），必要时把注册用户提升为管理员。
"""
from sqlalchemy import select

from app.models.ecommerce import Product, Sku, User


def _register(raw_client, username):
    r = raw_client.post(
        "/api/v1/auth/register", json={"username": username, "password": "secret1"}
    )
    assert r.status_code == 201, r.text
    return r.json()


def _login_token(raw_client, username):
    return raw_client.post(
        "/api/v1/auth/login", json={"username": username, "password": "secret1"}
    ).json()["access_token"]


def _make_admin(raw_client, db, username):
    """把一个已注册用户提升为管理员，返回其新令牌。"""
    u = db.execute(select(User).where(User.username == username)).scalars().first()
    assert u is not None
    u.is_admin = True
    db.commit()
    return _login_token(raw_client, username)


def _auth(raw_client, token):
    return {"Authorization": f"Bearer {token}"}


# ---------------- 用户管理：仅管理员 ----------------


def test_non_admin_cannot_create_user(raw_client, db):
    a = _register(raw_client, "rbac1")
    h = _auth(raw_client, _login_token(raw_client, "rbac1"))
    r = raw_client.post("/api/v1/users", headers=h, json={"username": "rbac_x"})
    assert r.status_code == 403


def test_admin_can_create_user(raw_client, db):
    _register(raw_client, "rbac2")
    h = _auth(raw_client, _make_admin(raw_client, db, "rbac2"))
    r = raw_client.post("/api/v1/users", headers=h, json={"username": "rbac_y"})
    assert r.status_code == 201, r.text


def test_non_admin_cannot_list_users(raw_client, db):
    _register(raw_client, "rbac3")
    h = _auth(raw_client, _login_token(raw_client, "rbac3"))
    assert raw_client.get("/api/v1/users", headers=h).status_code == 403


def test_non_admin_cannot_disable_other_user(raw_client, db):
    a = _register(raw_client, "rbacA")
    b = _register(raw_client, "rbacB")
    h = _auth(raw_client, _login_token(raw_client, "rbacB"))
    # 非本人非管理员改/删他人 -> 404（不泄露目标是否存在）
    assert (
        raw_client.patch(
            f"/api/v1/users/{a['id']}", headers=h, json={"is_active": False}
        ).status_code
        == 404
    )
    # 禁用是管理员专属操作，非管理员一律 403（无论是否本人）
    assert raw_client.delete(f"/api/v1/users/{a['id']}", headers=h).status_code == 403


def test_self_can_update_own_profile(raw_client, db):
    a = _register(raw_client, "rbacSelf")
    h = _auth(raw_client, _login_token(raw_client, "rbacSelf"))
    r = raw_client.patch(f"/api/v1/users/{a['id']}", headers=h, json={"nickname": "新昵称"})
    assert r.status_code == 200, r.text
    assert r.json()["nickname"] == "新昵称"


def test_non_admin_cannot_read_other_profile(raw_client, db):
    a = _register(raw_client, "rbacP1")
    b = _register(raw_client, "rbacP2")
    h = _auth(raw_client, _login_token(raw_client, "rbacP2"))
    assert raw_client.get(f"/api/v1/users/{a['id']}", headers=h).status_code == 404


# ---------------- 订单：买家仅见自己，流转仅管理员 ----------------


def _make_sku(db):
    p = Product(name="RBAC商品", status="on_sale")
    db.add(p)
    db.flush()
    sku = Sku(product_id=p.id, sku_code="RBAC-SKU", price=10, stock=100)
    db.add(sku)
    db.commit()
    return sku


def test_buyer_sees_own_orders_only(raw_client, db, order_flow):
    a = _register(raw_client, "buyerA")
    b = _register(raw_client, "buyerB")
    h_a = _auth(raw_client, _login_token(raw_client, "buyerA"))
    h_b = _auth(raw_client, _login_token(raw_client, "buyerB"))
    sku = _make_sku(db)

    raw_client.post("/api/v1/orders", headers=h_a, json={"items": [{"sku_id": sku.id, "quantity": 1}]})
    raw_client.post("/api/v1/orders", headers=h_a, json={"items": [{"sku_id": sku.id, "quantity": 1}]})
    raw_client.post("/api/v1/orders", headers=h_b, json={"items": [{"sku_id": sku.id, "quantity": 1}]})

    ra = raw_client.get("/api/v1/orders", headers=h_a).json()
    assert len(ra) == 2
    assert all(o["user_id"] == a["id"] for o in ra)

    rb = raw_client.get("/api/v1/orders", headers=h_b).json()
    assert len(rb) == 1
    assert rb[0]["user_id"] == b["id"]


def test_order_action_requires_admin(raw_client, db, order_flow):
    a = _register(raw_client, "opUser")
    h = _auth(raw_client, _login_token(raw_client, "opUser"))
    sku = _make_sku(db)
    order = raw_client.post(
        "/api/v1/orders", headers=h, json={"items": [{"sku_id": sku.id, "quantity": 1}]}
    ).json()

    # 非管理员推进流转 -> 403
    assert raw_client.post(f"/api/v1/orders/{order['id']}/actions/pay", headers=h).status_code == 403

    # 提升为管理员后才可推进
    h_admin = _auth(raw_client, _make_admin(raw_client, db, "opUser"))
    r = raw_client.post(f"/api/v1/orders/{order['id']}/actions/pay", headers=h_admin)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "paid"
