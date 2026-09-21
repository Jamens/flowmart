"""用户 / 地址 / 分类 接口测试。

重点验证几条容易出错的业务规则：
1. 默认地址只能有一个
2. 地址归属校验（不能越权改他人地址）
3. 删除用户是软删除，不破坏订单外键
4. 有商品引用的分类不能删除（category_id 非外键，数据库不会兜底）
"""
import pytest

from app.models.ecommerce import Category, Product, Sku, User


def create_user(client, username, **kw):
    return client.post("/api/v1/users", json={"username": username, **kw})


def test_create_and_list_users(client):
    assert create_user(client, "u1", nickname="用户一").status_code == 201
    users = client.get("/api/v1/users").json()
    assert any(u["username"] == "u1" for u in users)


def test_duplicate_username_rejected(client):
    assert create_user(client, "dup").status_code == 201
    r = create_user(client, "dup")
    assert r.status_code == 400
    assert "已存在" in r.json()["detail"]


def test_delete_user_is_soft(client):
    """禁用用户而非物理删除：订单外键仍在引用他。"""
    uid = create_user(client, "soft").json()["id"]
    r = client.delete(f"/api/v1/users/{uid}")
    assert r.status_code == 200
    assert r.json()["is_active"] is False
    # 用户仍可查到，只是被标记禁用
    assert client.get(f"/api/v1/users/{uid}").json()["is_active"] is False
    # active_only 过滤后不再出现
    assert all(u["id"] != uid for u in client.get("/api/v1/users?active_only=true").json())


def test_update_user(client):
    uid = create_user(client, "upd", nickname="旧名").json()["id"]
    r = client.patch(f"/api/v1/users/{uid}", json={"nickname": "新名"})
    assert r.status_code == 200
    assert client.get(f"/api/v1/users/{uid}").json()["nickname"] == "新名"


# ---------------- 地址 ----------------


def test_add_address(client):
    uid = client.user.id
    r = client.post(
        f"/api/v1/users/{uid}/addresses",
        json={"receiver": "张三", "phone": "13800000000", "city": "深圳"},
    )
    assert r.status_code == 201
    addrs = client.get(f"/api/v1/users/{uid}/addresses").json()
    assert len(addrs) == 1
    assert addrs[0]["receiver"] == "张三"


def test_default_address_is_exclusive(client):
    """默认地址只能有一个：设置新的默认后，旧的必须自动取消。"""
    uid = client.user.id
    base = {"receiver": "张三", "phone": "13800000000", "city": "深圳"}
    client.post(f"/api/v1/users/{uid}/addresses", json={**base, "is_default": True})
    client.post(f"/api/v1/users/{uid}/addresses", json={**base, "is_default": True})

    addrs = client.get(f"/api/v1/users/{uid}/addresses").json()
    assert len(addrs) == 2
    assert sum(1 for a in addrs if a["is_default"]) == 1
    # 后设置的那个才是默认
    assert addrs[1]["is_default"] is True


def test_cannot_touch_other_users_address(client, db):
    """地址归属令牌用户：以他人身份访问其地址应 404（_assert_owner 拦截）。"""
    u1_id = create_user(client, "owner").json()["id"]
    u2_id = create_user(client, "other").json()["id"]

    # 以 u1 的身份创建地址
    client.as_user(db.get(User, u1_id))
    addr_id = client.post(
        f"/api/v1/users/{u1_id}/addresses",
        json={"receiver": "张三", "phone": "13800000000"},
    ).json()["id"]

    # 以 u2 的身份尝试改/删 u1 的地址 -> 404
    client.as_user(db.get(User, u2_id))
    assert client.patch(
        f"/api/v1/users/{u1_id}/addresses/{addr_id}", json={"receiver": "黑客"}
    ).status_code == 404
    assert client.delete(f"/api/v1/users/{u1_id}/addresses/{addr_id}").status_code == 404

    # 以 u1 身份回看，原地址不受影响
    client.as_user(db.get(User, u1_id))
    assert client.get(f"/api/v1/users/{u1_id}/addresses").json()[0]["receiver"] == "张三"


def test_delete_address(client):
    uid = client.user.id
    addr_id = client.post(
        f"/api/v1/users/{uid}/addresses",
        json={"receiver": "张三", "phone": "13800000000"},
    ).json()["id"]
    assert client.delete(f"/api/v1/users/{uid}/addresses/{addr_id}").status_code == 200
    assert client.get(f"/api/v1/users/{uid}/addresses").json() == []


# ---------------- 分类 ----------------


def test_create_category_and_tree(client):
    parent = client.post("/api/v1/categories", json={"name": "电子产品"}).json()
    child = client.post(
        "/api/v1/categories", json={"name": "手机", "parent_id": parent["id"]}
    ).json()
    assert child["id"]

    tree = client.get("/api/v1/categories/tree").json()
    root = next(t for t in tree if t["id"] == parent["id"])
    assert len(root["children"]) == 1
    assert root["children"][0]["name"] == "手机"


def test_category_cannot_be_its_own_parent(client):
    cid = client.post("/api/v1/categories", json={"name": "自环分类"}).json()["id"]
    r = client.patch(f"/api/v1/categories/{cid}", json={"parent_id": cid})
    assert r.status_code == 400


def test_delete_category_with_products_rejected(client, db):
    """category_id 不是外键，数据库不会拦 —— 必须靠应用层校验。"""
    c = Category(name="有商品的分类")
    db.add(c)
    db.flush()
    p = Product(name="占用分类的商品", category_id=c.id)
    db.add(p)
    db.commit()

    r = client.delete(f"/api/v1/categories/{c.id}")
    assert r.status_code == 400
    assert "商品" in r.json()["detail"]
    # 分类仍在
    assert any(x["id"] == c.id for x in client.get("/api/v1/categories").json())


def test_delete_empty_category(client):
    cid = client.post("/api/v1/categories", json={"name": "空分类"}).json()["id"]
    assert client.delete(f"/api/v1/categories/{cid}").status_code == 200


def test_category_list_shows_product_count(client, db):
    c = Category(name="计数分类")
    db.add(c)
    db.flush()
    p = Product(name="商品A", category_id=c.id)
    db.add(p)
    db.flush()
    db.add(Sku(product_id=p.id, sku_code="CNT-1", price=10, stock=1))
    db.commit()

    row = next(x for x in client.get("/api/v1/categories").json() if x["id"] == c.id)
    assert row["product_count"] == 1


def test_indirect_cycle_rejected(client):
    """A→B 之后再把 B→A 会成环，必须拒绝。

    否则两个分类互相成为对方的 children，谁都不会出现在根节点，
    在分类树里凭空消失 —— 比直接报错难排查得多。
    """
    a = client.post("/api/v1/categories", json={"name": "环A"}).json()
    b = client.post("/api/v1/categories", json={"name": "环B"}).json()

    assert client.patch(
        f"/api/v1/categories/{a['id']}", json={"parent_id": b["id"]}
    ).status_code == 200

    r = client.patch(f"/api/v1/categories/{b['id']}", json={"parent_id": a["id"]})
    assert r.status_code == 400

    # B 必须仍在树里可见
    root_ids = [t["id"] for t in client.get("/api/v1/categories/tree").json()]
    assert b["id"] in root_ids
