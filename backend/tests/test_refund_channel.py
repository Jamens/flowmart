"""退款接真实渠道 API 的测试。

核心要守住的一条资金安全属性：**钱必须真退回去，订单才标已退款**。
- mock 渠道：发起即成功，验证订单能走到「已退款」并落退款单号；
- 微信 / 支付宝：用本地生成的 RSA 密钥把「签名请求 + 解析响应」这条链路完整跑通
  （真实渠道最容易出错的就是签名/验签，而它纯本地可测）；
- 渠道失败：绝不能把订单标成已退款（否则钱没退、订单却关了）。

不接真实网关（没有商户号也连不上），回调类测试同 test_payments.py 的本地密钥套路。
"""
import base64
import json

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509 import NameOID
from sqlalchemy import select

from app.core.config import settings
from app.core.payment import MockPaymentProvider, reset_payment_provider
from app.models.ecommerce import Order, Payment
from app.services.order_service import OrderService


@pytest.fixture(autouse=True)
def _reset_provider():
    """每个用例前后都丢掉渠道单例，避免上一个用例 monkeypatch 的配置泄漏到本例。"""
    reset_payment_provider()
    yield
    reset_payment_provider()


# ---------------- 本地密钥工具（与 test_payments.py 同套路） ----------------

def _gen_rsa(tmp_path, name):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = tmp_path / f"{name}_priv.pem"
    priv.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    pub = tmp_path / f"{name}_pub.pem"
    pub.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    return key, priv, pub


def _self_signed_cert(tmp_path, key, name="platform"):
    import datetime as _dt

    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Tenpay.com Test")])
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(days=1))
        .not_valid_after(now + _dt.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    path = tmp_path / f"{name}_cert.pem"
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return path


def _setup_wechat(monkeypatch, tmp_path):
    platform_key, _, _ = _gen_rsa(tmp_path, "wxplat")
    cert = _self_signed_cert(tmp_path, platform_key)
    _, mch_priv, _ = _gen_rsa(tmp_path, "wxmch")
    api_key = "k" * 32  # APIv3 密钥必须 32 字节（占位假值，勿误当真实凭据）
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "wechat")
    monkeypatch.setattr(settings, "WECHAT_APPID", "wxapp")
    monkeypatch.setattr(settings, "WECHAT_MCHID", "1234567890")
    monkeypatch.setattr(settings, "WECHAT_API_V3_KEY", api_key)
    monkeypatch.setattr(settings, "WECHAT_MCH_CERT_SERIAL_NO", "TESTSERIAL")
    monkeypatch.setattr(settings, "WECHAT_PRIVATE_KEY_PATH", str(mch_priv))
    monkeypatch.setattr(settings, "WECHAT_PLATFORM_CERT_PATH", str(cert))
    monkeypatch.setattr(settings, "PAYMENT_NOTIFY_BASE_URL", "https://example.com")
    reset_payment_provider()
    return platform_key, api_key


def _setup_alipay(monkeypatch, tmp_path):
    key, priv, pub = _gen_rsa(tmp_path, "alipay")
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "alipay")
    monkeypatch.setattr(settings, "ALIPAY_APPID", "test_app")
    monkeypatch.setattr(settings, "ALIPAY_PRIVATE_KEY_PATH", str(priv))
    monkeypatch.setattr(settings, "ALIPAY_PUBLIC_KEY_PATH", str(pub))
    monkeypatch.setattr(settings, "PAYMENT_NOTIFY_BASE_URL", "https://example.com")
    reset_payment_provider()
    return key


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    @property
    def text(self):
        return json.dumps(self._payload)


# ---------------- mock 渠道：端到端退款 ----------------

def _make_paid_order(db, payment_provider="mock"):
    """造一个已支付的订单（含成功流水），返回订单 id。供各渠道退款用例复用。"""
    from app.models.ecommerce import Product, Sku

    p = Product(name="退款测试商品", status="on_sale")
    db.add(p)
    db.flush()
    s = Sku(product_id=p.id, sku_code="RF-SKU", spec="默认", price=100, stock=10)
    db.add(s)
    db.flush()

    svc = OrderService(db)
    order = svc.create_order(user_id=1, items=[{"sku_id": s.id, "quantity": 1}])
    svc.trigger(order.id, "pay", operator="u1", comment="支付成功")
    return order.id


def test_mock_refund_records_refund_no(client, db):
    order_id = _make_paid_order(db, "mock")
    r = client.post(f"/api/v1/orders/{order_id}/actions/refund")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "closed"

    db.expire_all()
    pay = db.execute(
        select(Payment).where(Payment.order_id == order_id)
    ).scalar_one()
    assert pay.status == "refunded"
    assert pay.out_refund_no.startswith("RF"), "必须生成我方退款单号"
    # mock 渠道：把 out_refund_no 当作渠道退款单号回传，对账可见
    assert pay.refund_channel_no == pay.out_refund_no


# ---------------- 微信退款：签名请求 + 解析响应 ----------------

def test_wechat_refund_calls_channel_and_records_id(client, db, monkeypatch, tmp_path):
    _setup_wechat(monkeypatch, tmp_path)
    order_id = _make_paid_order(db, "wechat")

    # 补上真实回调才会有的渠道交易号
    pay = db.execute(select(Payment).where(Payment.order_id == order_id)).scalar_one()
    pay.provider_trade_no = "WX-TXN-1"
    db.commit()

    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _FakeResp(200, {"refund_id": "WX-REFUND-1", "status": "PROCESSING"})

    monkeypatch.setattr(httpx, "post", fake_post)

    r = client.post(f"/api/v1/orders/{order_id}/actions/refund")
    assert r.status_code == 200, r.text

    # 断言确实向微信退款接口发起了带签名的请求
    assert calls, "应当调用微信退款接口"
    url, kw = calls[0]
    assert url.endswith("/v3/refund/domestic/transactions/refunds")
    assert kw["headers"]["Authorization"].startswith("WECHATPAY2")
    assert "out_refund_no" in kw["content"]
    assert "WX-TXN-1" in kw["content"]  # transaction_id

    db.expire_all()
    pay = db.execute(select(Payment).where(Payment.order_id == order_id)).scalar_one()
    assert pay.status == "refunded"
    assert pay.refund_channel_no == "WX-REFUND-1"
    assert pay.out_refund_no.startswith("RF")


def test_wechat_refund_channel_failure_keeps_order_paid(client, db, monkeypatch, tmp_path):
    _setup_wechat(monkeypatch, tmp_path)
    order_id = _make_paid_order(db, "wechat")
    pay = db.execute(select(Payment).where(Payment.order_id == order_id)).scalar_one()
    pay.provider_trade_no = "WX-TXN-1"
    db.commit()

    def fake_post(url, **kwargs):
        return _FakeResp(500, {"message": "SYSTEM_ERROR"})

    monkeypatch.setattr(httpx, "post", fake_post)

    r = client.post(f"/api/v1/orders/{order_id}/actions/refund")
    # 渠道失败必须明确反馈，且绝不能把订单标成已退款
    assert r.status_code == 502, r.text

    db.expire_all()
    assert db.get(Order, order_id).status == "paid", "钱没退，订单不能关"
    pay = db.execute(select(Payment).where(Payment.order_id == order_id)).scalar_one()
    assert pay.status == "success"
    assert pay.refund_channel_no is None


# ---------------- 支付宝退款：签名请求 + 验签响应 ----------------

def _alipay_refund_response(key, *, code="10000", trade_no="ALI-TXN-1",
                            out_trade_no="PAY-ALI-1", refund_fee="100.00", sub_msg=""):
    inner = {
        "code": code,
        "msg": "Success" if code == "10000" else "Business Failed",
        "trade_no": trade_no,
        "out_trade_no": out_trade_no,
        "refund_fee": refund_fee,
    }
    if code != "10000":
        inner["sub_code"] = "TRADE_NOT_EXIST"
        inner["sub_msg"] = sub_msg or "交易不存在"
    # 用配置的支付宝公钥对应的私钥对响应签名（测试桩模拟「支付宝回签」）
    inner_str = json.dumps(inner, separators=(",", ":"), ensure_ascii=False)
    sig = key.sign(inner_str.encode(), padding.PKCS1v15(), hashes.SHA256())
    return {"alipay_trade_refund_response": inner, "sign": base64.b64encode(sig).decode()}


def test_alipay_refund_calls_channel_and_records_id(client, db, order_flow, monkeypatch, tmp_path):
    key = _setup_alipay(monkeypatch, tmp_path)
    order_id = _make_paid_order(db, "alipay")
    pay = db.execute(select(Payment).where(Payment.order_id == order_id)).scalar_one()
    pay.provider_trade_no = "ALI-TXN-1"
    db.commit()

    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _FakeResp(200, _alipay_refund_response(key))

    monkeypatch.setattr(httpx, "post", fake_post)

    r = client.post(f"/api/v1/orders/{order_id}/actions/refund")
    assert r.status_code == 200, r.text

    assert calls, "应当调用支付宝退款接口"
    url, kw = calls[0]
    assert url == settings.ALIPAY_GATEWAY
    # 我方 out_refund_no 作为重试幂等键，嵌在 biz_content 里
    biz = json.loads(kw["data"]["biz_content"])
    assert biz["out_request_no"].startswith("RF")
    assert biz["trade_no"] == "ALI-TXN-1"

    db.expire_all()
    pay = db.execute(select(Payment).where(Payment.order_id == order_id)).scalar_one()
    assert pay.status == "refunded"
    assert pay.refund_channel_no == "ALI-TXN-1"
    assert pay.out_refund_no.startswith("RF")


def test_alipay_refund_business_failure_keeps_order_paid(client, db, monkeypatch, tmp_path):
    key = _setup_alipay(monkeypatch, tmp_path)
    order_id = _make_paid_order(db, "alipay")
    pay = db.execute(select(Payment).where(Payment.order_id == order_id)).scalar_one()
    pay.provider_trade_no = "ALI-TXN-1"
    db.commit()

    def fake_post(url, **kwargs):
        # 渠道返回业务失败（已正确验签），必须拒绝而非假装成功
        return _FakeResp(200, _alipay_refund_response(key, code="40004", sub_msg="交易不存在"))

    monkeypatch.setattr(httpx, "post", fake_post)

    r = client.post(f"/api/v1/orders/{order_id}/actions/refund")
    assert r.status_code == 502, r.text

    db.expire_all()
    assert db.get(Order, order_id).status == "paid"
    pay = db.execute(select(Payment).where(Payment.order_id == order_id)).scalar_one()
    assert pay.status == "success"


# ---------------- 幂等：复用 out_refund_no ----------------

def test_refund_reuses_existing_out_refund_no(db, order_flow, monkeypatch):
    """若流水已有 out_refund_no（重试场景），再次退款必须复用，避免渠道侧重复退款。"""
    order_id = _make_paid_order(db, "mock")
    pay = db.execute(select(Payment).where(Payment.order_id == order_id)).scalar_one()
    pay.out_refund_no = "RF-FIXED-1"
    db.commit()

    recorded = {}

    def fake_refund(self, *, out_trade_no, provider_trade_no, amount, reason, out_refund_no):
        recorded["out_refund_no"] = out_refund_no
        from app.core.payment import RefundResult
        return RefundResult(refund_id=out_refund_no, success=True)

    monkeypatch.setattr(MockPaymentProvider, "refund", fake_refund)

    OrderService(db).trigger(order_id, "refund", operator="admin", comment="退款")
    assert recorded["out_refund_no"] == "RF-FIXED-1"
