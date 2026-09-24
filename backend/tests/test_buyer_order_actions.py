"""买家侧订单动作：取消订单 / 确认收货。

背景（为什么要有这个文件）：推进流转的端点原先写死「仅管理员」，但服务层
`OrderService.cancel()` 的默认 operator 就是 "user"（order_service.py:297），
说明取消本就是按买家自助设计的，API 层却没暴露——服务层有、接口层没接通。

本文件锁定「管理员全能 + 买家仅白名单事件、且仅限自己的订单」这套权限分流，
并覆盖三个容易回归的点：
  1. 他人订单要 404 而不是 403（与订单详情一致，不泄露订单是否存在）；
  2. operator 取自认证身份，body 里伪造无效（否则可污染流转审计轨迹）；
  3. 白名单只管「身份能不能做」，节点合法性仍由引擎判定。
"""
from sqlalchemy import select

import pytest

from app.core.security import hash_password
from app.models.ecommerce import Product, Sku, User
from app.models.workflow import WorkflowTransitionLog


@pytest.fixture
def sku(db):
    """一个可用于下单的 SKU（库存 10）。"""
    p = Product(name="买家动作测试商品", status="on_sale")
    db.add(p)
    db.flush()
    s = Sku(product_id=p.id, sku_code="BUYER-SKU", spec="默认", price=100, stock=10)
    db.add(s)
    db.commit()
    return s


def _make_buyer(db, username="buyer_x"):
    """一个已验证联系方式的普通买家（非管理员）。"""
    u = User(username=username, nickname=username, phone="13800000123",
             password_hash=hash_password("123456"), is_admin=False, email_verified=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _new_order(client, sku, quantity=1):
    """以「当前登录身份」下一单，返回订单 dict。"""
    r = client.post("/api/v1/orders",
                    json={"items": [{"sku_id": sku.id, "quantity": quantity}]})
    assert r.status_code == 201, r.text
    return r.json()


def test_buyer_can_cancel_own_order(buyer_client, sku):
    """买家可自助取消自己「待付款」的订单。"""
    order = _new_order(buyer_client, sku)
    assert order["current_node_key"] == "pending_payment"

    r = buyer_client.post(f"/api/v1/orders/{order['id']}/actions/cancel")
    assert r.status_code == 200, r.text
    assert r.json()["current_node_key"] == "closed"


def test_buyer_cancel_returns_stock(buyer_client, sku, db):
    """取消必须归还库存，否则库存会凭空消失。"""
    order = _new_order(buyer_client, sku)
    db.refresh(sku)
    assert sku.stock == 9, "下单应先扣减库存"

    assert buyer_client.post(
        f"/api/v1/orders/{order['id']}/actions/cancel"
    ).status_code == 200

    db.refresh(sku)
    assert sku.stock == 10, "取消后应归还库存"


def test_buyer_can_confirm_own_shipped_order(client, db, sku):
    """买家可对自己「已发货」的订单确认收货（管理员先代为支付、发货）。"""
    buyer = _make_buyer(db)
    client.as_user(buyer)
    oid = _new_order(client, sku)["id"]

    # 管理员完成支付与发货
    client.as_user(client.user)
    assert client.post(f"/api/v1/orders/{oid}/actions/pay").status_code == 200
    assert client.post(f"/api/v1/orders/{oid}/actions/ship").status_code == 200

    # 买家确认收货
    client.as_user(buyer)
    r = client.post(f"/api/v1/orders/{oid}/actions/confirm")
    assert r.status_code == 200, r.text
    assert r.json()["current_node_key"] == "completed"


def test_buyer_cannot_fire_admin_only_events(buyer_client, sku):
    """白名单默认拒绝：pay/ship 等运营事件即使流程允许，买家也触发不了。"""
    order = _new_order(buyer_client, sku)
    for ev in ("pay", "ship"):
        r = buyer_client.post(f"/api/v1/orders/{order['id']}/actions/{ev}")
        assert r.status_code == 403, (ev, r.text)


def test_buyer_cannot_act_on_others_order(client, db, sku):
    """他人订单的越权动作统一 404，而不是 403——与订单详情一致，不泄露订单是否存在。"""
    admin_order = _new_order(client, sku)  # 管理员自己的订单
    buyer = _make_buyer(db)

    client.as_user(buyer)
    r = client.post(f"/api/v1/orders/{admin_order['id']}/actions/cancel")
    assert r.status_code == 404
    assert r.json()["detail"] == "订单不存在"


def test_admin_can_still_fire_any_event_on_any_order(client, db, sku):
    """回归：管理员不受白名单限制，可对任意订单（含他人的）执行任意事件。"""
    buyer = _make_buyer(db)
    client.as_user(buyer)
    oid = _new_order(client, sku)["id"]

    client.as_user(client.user)
    r = client.post(f"/api/v1/orders/{oid}/actions/pay")
    assert r.status_code == 200, r.text
    assert r.json()["current_node_key"] == "paid"


def test_operator_is_identity_not_request_body(buyer_client, db, sku):
    """operator 取自认证身份，不信任请求体：body 里伪造 operator=admin 无效。"""
    order = _new_order(buyer_client, sku)
    r = buyer_client.post(
        f"/api/v1/orders/{order['id']}/actions/cancel",
        json={"operator": "admin", "comment": "我不想要了"},
    )
    assert r.status_code == 200, r.text

    # 按 instance_id 收敛，而不是取全局最新一条：同用例内若出现多次取消也不会误判
    log = db.execute(
        select(WorkflowTransitionLog)
        .where(
            WorkflowTransitionLog.event == "cancel",
            WorkflowTransitionLog.instance_id == order["workflow_instance_id"],
        )
        .order_by(WorkflowTransitionLog.id.desc())
    ).scalars().first()
    assert log is not None
    assert log.operator == buyer_client.user.username
    assert log.operator != "admin", "请求体里的 operator 不应被采信"


def test_ownership_is_checked_before_whitelist(client, db, sku):
    """归属判断必须先于白名单判断：他人订单 + 非白名单事件要 404，而不是 403。

    顺序一旦调换，403 就会泄露「该订单确实存在」，与项目「不泄露存在性」的约定冲突。
    """
    admin_order = _new_order(client, sku)
    buyer = _make_buyer(db)

    client.as_user(buyer)
    r = client.post(f"/api/v1/orders/{admin_order['id']}/actions/pay")
    assert r.status_code == 404, "他人订单应统一 404，不能因事件不在白名单而返回 403"


def test_whitelist_is_exact_match(buyer_client, sku):
    """白名单精确匹配、默认拒绝：大小写变体不算数。

    防止后人加 `.lower()` 归一化或改成从流程定义里取事件集合——
    那会让白名单随配置漂移，失去「权限策略集中可见可审计」的价值。
    """
    order = _new_order(buyer_client, sku)
    r = buyer_client.post(f"/api/v1/orders/{order['id']}/actions/Cancel")
    assert r.status_code == 403, r.text


def test_admin_operator_also_from_identity(client, db, sku):
    """管理员同样按认证身份归因，不再采信请求体（审计口径双向锁定）。"""
    order = _new_order(client, sku)
    r = client.post(
        f"/api/v1/orders/{order['id']}/actions/pay",
        json={"operator": "someone_else"},
    )
    assert r.status_code == 200, r.text

    log = db.execute(
        select(WorkflowTransitionLog)
        .where(
            WorkflowTransitionLog.event == "pay",
            WorkflowTransitionLog.instance_id == order["workflow_instance_id"],
        )
        .order_by(WorkflowTransitionLog.id.desc())
    ).scalars().first()
    assert log is not None
    assert log.operator == client.user.username
    assert log.operator != "someone_else"


def test_buyer_event_still_validated_by_engine(buyer_client, sku):
    """白名单只管「身份能不能做」，节点合法性仍由引擎判定：待付款不能直接确认收货。"""
    order = _new_order(buyer_client, sku)
    r = buyer_client.post(f"/api/v1/orders/{order['id']}/actions/confirm")
    assert r.status_code == 400
    assert "不可执行" in r.json()["detail"]


def test_anonymous_cannot_fire_event(raw_client):
    """未登录 401：权限分流的前提是先有身份。"""
    assert raw_client.post("/api/v1/orders/1/actions/cancel").status_code == 401
