"""资金安全：pay 只能由支付渠道异步回调触发，禁止经 `fire_event` 手动标记已付款。

这是「0 元提货」漏洞的修复——管理员/任意登录用户在订单抽屉里点一下「支付」，
订单就变成已付款而钱根本没到账。非 mock 支付渠道下必须拦截；mock 是演示/联调，
无真实资金，保留手动推进以便调试。

本文件不发起/回调任何真实支付，只校验 `fire_event` 与 available_events 这两道闸门，
因此不需要真实商户密钥（直接读 `settings.PAYMENT_PROVIDER` 配置串，不构造 provider）。
"""
import pytest

from app.core.config import settings
from app.models.ecommerce import Product, Sku


@pytest.fixture
def sku(db):
    """一个可用于下单的 SKU（库存 10）。"""
    p = Product(name="手动支付闸门测试商品", status="on_sale")
    db.add(p)
    db.flush()
    s = Sku(product_id=p.id, sku_code="PAYGUARD-SKU", spec="默认", price=100, stock=10)
    db.add(s)
    db.commit()
    return s


@pytest.fixture
def real_channel(monkeypatch):
    """把支付渠道切到非 mock（alipay）。无需真实密钥：本文件不构造支付 provider。"""
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "alipay")
    yield
    # monkeypatch 自动还原 settings；本文件未调用 get_payment_provider()，无需 reset。


def _new_order(client, sku, quantity=1):
    r = client.post(
        "/api/v1/orders",
        json={"items": [{"sku_id": sku.id, "quantity": quantity}]},
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_admin_manual_pay_forbidden_when_real_channel(real_channel, client, sku):
    """非 mock 渠道：管理员手动 fire pay 必须 403，钱没到账不能把订单标成已付。"""
    order = _new_order(client, sku)
    r = client.post(f"/api/v1/orders/{order['id']}/actions/pay")
    assert r.status_code == 403, r.text
    assert "回调" in r.json()["detail"]

    # 订单确实没有被推进：仍停在待付款，且流转时间线里没有任何 pay 记录
    detail = client.get(f"/api/v1/orders/{order['id']}").json()
    assert detail["current_node_key"] == "pending_payment"
    assert all(t["event"] != "pay" for t in detail["timeline"])


def test_admin_manual_pay_allowed_in_mock(client, sku):
    """mock 渠道：保留手动推进，便于演示/联调（回归：原本就允许的行为不能丢）。"""
    order = _new_order(client, sku)
    r = client.post(f"/api/v1/orders/{order['id']}/actions/pay")
    assert r.status_code == 200, r.text
    assert r.json()["current_node_key"] == "paid"


def test_buyer_manual_pay_forbidden_when_real_channel(real_channel, buyer_client, sku):
    """非 mock 渠道：买家（本就不在白名单）手动 fire pay 仍 403，且不泄露订单存在性。"""
    order = _new_order(buyer_client, sku)
    r = buyer_client.post(f"/api/v1/orders/{order['id']}/actions/pay")
    assert r.status_code == 403, r.text


def test_available_events_excludes_pay_when_real_channel(real_channel, client, sku):
    """非 mock 渠道：available_events 不返回 pay，前端抽屉不会渲染「手动支付」按钮。"""
    order = _new_order(client, sku)
    detail = client.get(f"/api/v1/orders/{order['id']}").json()
    events = [e["event"] for e in detail["available_events"]]
    assert "pay" not in events
    # 取消分支不受影响，仍可手动取消
    assert "cancel" in events


def test_available_events_includes_pay_in_mock(client, sku):
    """mock 渠道：available_events 仍含 pay（回归：演示/联调需要它）。"""
    order = _new_order(client, sku)
    detail = client.get(f"/api/v1/orders/{order['id']}").json()
    events = [e["event"] for e in detail["available_events"]]
    assert "pay" in events
