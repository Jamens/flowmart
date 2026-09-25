"""写操作管理员闸门：分类 / 流程定义 / admin_db。

这些端点此前只要求「登录」，意味着**任意买家**都能：
  - 删分类、灌垃圾节点、把在售商品的归类清空
  - 改写并发布订单状态机（流程定义）——严重性高于商品下架
  - 枚举库表名（admin_db）

本文件锁定它们已收紧为 require_admin，并守住「管理员能力不变」的回归。

注意：需要双身份的用例一律用 `client` + `client.as_user(buyer)` 切换——
`client` 与 `buyer_client` 都覆盖 get_current_user，同一用例里同时请求会互相覆盖。
"""
from app.core.security import hash_password
from app.models.ecommerce import User


def _make_buyer(db, username="buyer_w") -> User:
    u = User(username=username, nickname=username, phone="13800000567",
             password_hash=hash_password("123456"), is_admin=False, email_verified=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _mk_definition(client, code):
    r = client.post("/api/v1/workflows/definitions", json={"code": code, "name": "测试流程"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------- 分类：买家一律 403 ----------------


def test_buyer_cannot_create_category(buyer_client):
    assert buyer_client.post("/api/v1/categories", json={"name": "垃圾分类"}).status_code == 403


def test_buyer_cannot_update_category(client, db):
    cid = client.post("/api/v1/categories", json={"name": "原分类"}).json()["id"]
    client.as_user(_make_buyer(db))
    assert client.patch(
        f"/api/v1/categories/{cid}", json={"name": "被篡改"}
    ).status_code == 403


def test_buyer_cannot_delete_category(client, db):
    cid = client.post("/api/v1/categories", json={"name": "待删分类"}).json()["id"]
    client.as_user(_make_buyer(db))
    assert client.delete(f"/api/v1/categories/{cid}").status_code == 403


# ---------------- 流程定义：买家一律 403 ----------------


def test_buyer_cannot_create_definition(buyer_client):
    assert buyer_client.post(
        "/api/v1/workflows/definitions", json={"code": "wf_hack", "name": "黑客流程"}
    ).status_code == 403


def test_buyer_cannot_update_definition(client, db):
    did = _mk_definition(client, "wf_upd")
    client.as_user(_make_buyer(db))
    r = client.put(
        f"/api/v1/workflows/definitions/{did}",
        json={"code": "wf_upd", "name": "被改写", "nodes": [], "transitions": []},
    )
    assert r.status_code == 403


def test_buyer_cannot_publish_definition(client, db):
    """发布是最危险的一步：它决定订单实际按哪张图流转。"""
    did = _mk_definition(client, "wf_pub")
    client.as_user(_make_buyer(db))
    assert client.post(f"/api/v1/workflows/definitions/{did}/publish").status_code == 403


def test_buyer_cannot_fork_definition(client, db):
    did = _mk_definition(client, "wf_fork")
    client.as_user(_make_buyer(db))
    assert client.post(f"/api/v1/workflows/definitions/{did}/versions").status_code == 403


def test_buyer_cannot_archive_definition(client, db):
    did = _mk_definition(client, "wf_arch")
    client.as_user(_make_buyer(db))
    assert client.delete(f"/api/v1/workflows/definitions/{did}").status_code == 403


# ---------------- admin_db：买家一律 403 ----------------


def test_buyer_cannot_list_tables(buyer_client):
    """枚举库表名属于运维能力，不能对普通买家开放。"""
    assert buyer_client.get("/api/v1/admin/db/tables").status_code == 403


# ---------------- 回归：管理员能力不变 ----------------


def test_admin_can_still_manage_categories(client):
    r = client.post("/api/v1/categories", json={"name": "管理员分类"})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert client.patch(
        f"/api/v1/categories/{cid}", json={"name": "改名"}
    ).status_code == 200
    assert client.delete(f"/api/v1/categories/{cid}").status_code == 200


def test_admin_can_still_manage_definitions(client):
    did = _mk_definition(client, "wf_admin")
    assert client.put(
        f"/api/v1/workflows/definitions/{did}",
        json={"code": "wf_admin", "name": "改名", "nodes": [], "transitions": []},
    ).status_code == 200
    assert client.post(f"/api/v1/workflows/definitions/{did}/versions").status_code == 201
    assert client.delete(f"/api/v1/workflows/definitions/{did}").status_code == 200


def test_admin_can_still_list_tables(client):
    assert client.get("/api/v1/admin/db/tables").status_code == 200
