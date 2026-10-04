"""支付渠道路由：发起支付 + 回调验签 + 幂等 + 金额校验。

本文件刻意**不接真实网关**（没有商户号也连不上），而是用本地生成的 RSA 密钥对
把「签名 / 验签 / 解密」这条链路完整跑一遍——真实渠道最容易出错的就是这几步，
而它们恰恰是纯本地可测的：签名与验签只依赖密钥，不依赖网络。

真正要守住的是四条安全属性（其余都是实现细节，错了会报错；这几条错了会**丢钱**）：
  1. 买家**不能**自己把订单推进成已支付（`pay` 不在买家白名单）；
  2. 回调**验签失败**一律不推进订单；
  3. 回调**幂等**——渠道重发多少次都只推进一次；
  4. 回调**金额必须一致**——改金额等于让攻击者自己定价。
"""
import base64
import json
from urllib.parse import urlencode

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.x509.oid import NameOID
from sqlalchemy import select

from app.core.config import settings
from app.core.payment import reset_payment_provider
from app.models.ecommerce import Order, Payment, Product, Sku


@pytest.fixture(autouse=True)
def _reset_provider():
    """每个用例后丢掉渠道单例，避免上一个用例 monkeypatch 的配置泄漏。"""
    yield
    reset_payment_provider()


@pytest.fixture
def sku(db):
    """一个可用于下单的 SKU（单价 100、库存 10）。"""
    p = Product(name="支付测试商品", status="on_sale")
    db.add(p)
    db.flush()
    s = Sku(product_id=p.id, sku_code="PAY-SKU", spec="默认", price=100, stock=10)
    db.add(s)
    db.commit()
    return s


def _new_order(client, sku, quantity=1):
    r = client.post("/api/v1/orders", json={"items": [{"sku_id": sku.id, "quantity": quantity}]})
    assert r.status_code == 201, r.text
    return r.json()


def _gen_rsa(tmp_path, name):
    """生成一对 RSA 密钥，返回 (私钥对象, 私钥路径, 公钥路径)。"""
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
    """用给定私钥签一张自签证书（微信回调验签用的是**证书里的公钥**）。

    有效期必须显式给：cryptography 不再为省略的 not_valid_before/after 兜底，
    不设会直接抛 ValueError。
    """
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


def _pending_payment(db, order, pay_no, channel, amount="100.00"):
    """插一条待支付流水——模拟「已向渠道发起过支付」的库内状态。"""
    from decimal import Decimal

    p = Payment(
        order_id=order["id"], pay_no=pay_no, amount=Decimal(amount),
        channel=channel, status="pending",
    )
    db.add(p)
    db.commit()
    return p


# ---------------- mock 渠道：发起即成功 ----------------

def test_buyer_can_initiate_payment_in_mock(buyer_client, sku, db):
    """mock 渠道下买家可发起支付，订单推进到「待发货」，且只产生一条成功流水。"""
    order = _new_order(buyer_client, sku)
    assert order["current_node_key"] == "pending_payment"

    r = buyer_client.post(f"/api/v1/orders/{order['id']}/payments")
    assert r.status_code == 200, r.text
    assert r.json()["paid"] is True

    db.expire_all()
    rows = db.execute(
        select(Payment).where(Payment.order_id == order["id"])
    ).scalars().all()
    assert len(rows) == 1, "同一笔订单只能有一条支付流水，重复会被对账算成收两次钱"
    assert rows[0].status == "success"
    assert db.get(Order, order["id"]).current_node_key == "paid"


def test_buyer_cannot_self_trigger_pay_action(buyer_client, sku):
    """铁律：买家不能自己把订单推进成已支付（否则等于 0 元提货）。"""
    order = _new_order(buyer_client, sku)
    r = buyer_client.post(f"/api/v1/orders/{order['id']}/actions/pay")
    assert r.status_code == 403, "pay 绝不能进 BUYER_ALLOWED_EVENTS"
    assert "买家不可执行" in r.json()["detail"]


def test_cannot_pay_others_order(buyer_client, sku, db):
    """他人订单发起支付 → 404（与「不存在」同等处理，不泄露订单是否存在）。"""
    order = _new_order(buyer_client, sku)
    # 把订单改挂到别的 user_id 上，模拟他人订单
    db.execute(
        Order.__table__.update().where(Order.id == order["id"]).values(user_id=999999)
    )
    db.commit()
    r = buyer_client.post(f"/api/v1/orders/{order['id']}/payments")
    assert r.status_code == 404


# ---------------- 支付宝回调：验签 / 幂等 / 金额 ----------------

def _alipay_body(key, params):
    """按支付宝规则对待签名串做 RSA2 签名，返回 form 编码的回调报文。"""
    content = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    sig = key.sign(content.encode(), padding.PKCS1v15(), hashes.SHA256())
    signed = dict(params, sign_type="RSA2", sign=base64.b64encode(sig).decode())
    return urlencode(signed).encode()


def _use_alipay(monkeypatch, tmp_path):
    key, priv, pub = _gen_rsa(tmp_path, "alipay")
    monkeypatch.setattr(settings, "PAYMENT_PROVIDER", "alipay")
    monkeypatch.setattr(settings, "ALIPAY_APPID", "test_app")
    monkeypatch.setattr(settings, "ALIPAY_PRIVATE_KEY_PATH", str(priv))
    monkeypatch.setattr(settings, "ALIPAY_PUBLIC_KEY_PATH", str(pub))
    monkeypatch.setattr(settings, "PAYMENT_NOTIFY_BASE_URL", "https://example.com")
    reset_payment_provider()
    return key


def test_alipay_callback_advances_order(buyer_client, sku, db, monkeypatch, tmp_path):
    """验签通过的支付宝回调推进订单，并记录渠道交易号用于对账。"""
    key = _use_alipay(monkeypatch, tmp_path)
    order = _new_order(buyer_client, sku)
    _pending_payment(db, order, "PAY-ALI-1", "alipay")

    body = _alipay_body(key, {
        "out_trade_no": "PAY-ALI-1",
        "trade_no": "ALIPAY-TXN-1",
        "total_amount": "100.00",
        "trade_status": "TRADE_SUCCESS",
    })
    r = buyer_client.post(
        "/api/v1/payments/notify/alipay",
        content=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200, r.text
    assert r.text == "success"

    db.expire_all()
    assert db.get(Order, order["id"]).current_node_key == "paid"
    pay = db.execute(
        select(Payment).where(Payment.pay_no == "PAY-ALI-1")
    ).scalar_one()
    assert pay.status == "success"
    assert pay.provider_trade_no == "ALIPAY-TXN-1", "对账全靠渠道交易号"


def test_alipay_callback_rejects_tampered_amount(buyer_client, sku, db, monkeypatch, tmp_path):
    """改过金额的回调验签不过 —— 订单绝不能被推进。"""
    key = _use_alipay(monkeypatch, tmp_path)
    order = _new_order(buyer_client, sku)
    _pending_payment(db, order, "PAY-ALI-2", "alipay")

    signed = _alipay_body(key, {
        "out_trade_no": "PAY-ALI-2",
        "trade_no": "T1",
        "total_amount": "100.00",
        "trade_status": "TRADE_SUCCESS",
    })
    # 签名之后再把 100.00 改成 0.01：金额被篡改，签名必然对不上
    tampered = signed.replace(b"total_amount=100.00", b"total_amount=0.01")

    r = buyer_client.post(
        "/api/v1/payments/notify/alipay",
        content=tampered,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 400, "验签失败必须拒绝"
    db.expire_all()
    assert db.get(Order, order["id"]).current_node_key == "pending_payment", "订单绝不能推进"


def test_alipay_callback_is_idempotent(buyer_client, sku, db, monkeypatch, tmp_path):
    """渠道重发同一回调：只推进一次，不产生第二条流水。"""
    key = _use_alipay(monkeypatch, tmp_path)
    order = _new_order(buyer_client, sku)
    _pending_payment(db, order, "PAY-ALI-3", "alipay")
    body = _alipay_body(key, {
        "out_trade_no": "PAY-ALI-3", "trade_no": "T3",
        "total_amount": "100.00", "trade_status": "TRADE_SUCCESS",
    })
    for _ in range(3):
        r = buyer_client.post(
            "/api/v1/payments/notify/alipay",
            content=body,
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert r.status_code == 200, r.text

    db.expire_all()
    rows = db.execute(
        select(Payment).where(Payment.order_id == order["id"])
    ).scalars().all()
    assert len(rows) == 1 and rows[0].status == "success"


def test_alipay_callback_rejects_amount_mismatch(buyer_client, sku, db, monkeypatch, tmp_path):
    """签名有效但金额与应收不符（例如渠道侧配错）→ 拒绝，不默默放行。"""
    key = _use_alipay(monkeypatch, tmp_path)
    order = _new_order(buyer_client, sku)
    _pending_payment(db, order, "PAY-ALI-4", "alipay")
    # 签名是自签的（有效），但金额只有 1 分钱
    body = _alipay_body(key, {
        "out_trade_no": "PAY-ALI-4", "trade_no": "T4",
        "total_amount": "0.01", "trade_status": "TRADE_SUCCESS",
    })
    r = buyer_client.post(
        "/api/v1/payments/notify/alipay",
        content=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 400
    assert "金额" in r.json()["detail"]
    db.expire_all()
    assert db.get(Order, order["id"]).current_node_key == "pending_payment"


# ---------------- 微信回调：验签 + AES-GCM 解密 ----------------

def _wechat_body(platform_key, plain, api_v3_key, ts="1700000000", nonce="nonce123456"):
    """构造微信回调报文：resource 用 AES-256-GCM 加密，整体再 RSA 签名。"""
    aad = "transaction"
    enc = Cipher(
        algorithms.AES(api_v3_key.encode()), modes.GCM(nonce.encode())
    ).encryptor()
    enc.authenticate_additional_data(aad.encode())
    ct = enc.update(json.dumps(plain, separators=(",", ":")).encode()) + enc.finalize()
    # 微信把 16 字节 authTag 拼在密文尾部
    resource = {
        "algorithm": "AEAD_AES_256_GCM",
        "ciphertext": base64.b64encode(ct + enc.tag).decode(),
        "nonce": nonce,
        "associated_data": aad,
    }
    body = json.dumps({"resource": resource}, separators=(",", ":"))
    message = f"{ts}\n{nonce}\n{body}\n"
    sig = platform_key.sign(message.encode(), padding.PKCS1v15(), hashes.SHA256())
    return body.encode(), {
        "wechatpay-timestamp": ts,
        "wechatpay-nonce": nonce,
        "wechatpay-signature": base64.b64encode(sig).decode(),
        "wechatpay-serial": "TESTSERIAL",
    }


def _use_wechat(monkeypatch, tmp_path):
    platform_key, _, _ = _gen_rsa(tmp_path, "wxplat")
    cert = _self_signed_cert(tmp_path, platform_key)
    _, mch_priv, _ = _gen_rsa(tmp_path, "wxmch")
    api_key = "0" * 32  # APIv3 密钥必须 32 字节
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


def test_wechat_callback_decrypts_and_advances(buyer_client, sku, db, monkeypatch, tmp_path):
    """微信回调：验签 + AES-GCM 解密后推进订单（金额按「分」换算成元）。"""
    platform_key, api_key = _use_wechat(monkeypatch, tmp_path)
    order = _new_order(buyer_client, sku)
    _pending_payment(db, order, "PAY-WX-1", "wechat")

    body, headers = _wechat_body(platform_key, {
        "out_trade_no": "PAY-WX-1",
        "transaction_id": "WX-TXN-1",
        "trade_state": "SUCCESS",
        "amount": {"total": 10000, "currency": "CNY"},  # 100.00 元
    }, api_key)
    r = buyer_client.post(
        "/api/v1/payments/notify/wechat", content=body,
        headers={"content-type": "application/json", **headers},
    )
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(Order, order["id"]).current_node_key == "paid"
    pay = db.execute(select(Payment).where(Payment.pay_no == "PAY-WX-1")).scalar_one()
    assert pay.provider_trade_no == "WX-TXN-1"


def test_wechat_callback_rejects_bad_signature(buyer_client, sku, db, monkeypatch, tmp_path):
    """伪造签名（用另一把钥匙签）→ 拒绝，订单不动。"""
    _use_wechat(monkeypatch, tmp_path)
    order = _new_order(buyer_client, sku)
    _pending_payment(db, order, "PAY-WX-2", "wechat")
    # 攻击者用自己的钥匙签名，而不是微信平台的
    attacker_key, _, _ = _gen_rsa(tmp_path, "attacker")
    body, headers = _wechat_body(attacker_key, {
        "out_trade_no": "PAY-WX-2", "transaction_id": "FAKE",
        "trade_state": "SUCCESS", "amount": {"total": 10000},
    }, "0" * 32)
    r = buyer_client.post(
        "/api/v1/payments/notify/wechat", content=body,
        headers={"content-type": "application/json", **headers},
    )
    assert r.status_code == 400
    db.expire_all()
    assert db.get(Order, order["id"]).current_node_key == "pending_payment"


def test_notify_channel_mismatch_is_404(buyer_client, sku, monkeypatch, tmp_path):
    """渠道与当前配置不符（如配置微信却收到 /notify/alipay）→ 不处理。"""
    _use_wechat(monkeypatch, tmp_path)
    assert buyer_client.post("/api/v1/payments/notify/alipay", content=b"x").status_code == 404


def test_alipay_callback_unknown_out_trade_no_returns_success(buyer_client, sku, db, monkeypatch, tmp_path):
    """验签成功但查不到对应流水：回成功**停止重发**（而非 404 让渠道死磕数小时），
    且不凭空建单、不推进任何订单。"""
    key = _use_alipay(monkeypatch, tmp_path)
    order = _new_order(buyer_client, sku)
    # 故意用一个库里不存在的 out_trade_no，但不建 pending 流水
    body = _alipay_body(key, {
        "out_trade_no": "PAY-NONEXISTENT",
        "trade_no": "ALIPAY-TXN-X",
        "total_amount": "100.00",
        "trade_status": "TRADE_SUCCESS",
    })
    r = buyer_client.post(
        "/api/v1/payments/notify/alipay",
        content=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200, r.text
    assert r.text == "success"
    # 不能凭空造单：库里没有 PAY-NONEXISTENT 这条流水，原订单状态也不变
    db.expire_all()
    assert db.execute(
        select(Payment).where(Payment.pay_no == "PAY-NONEXISTENT")
    ).scalar_one_or_none() is None
    assert db.get(Order, order["id"]).current_node_key == "pending_payment"


def test_wechat_callback_unknown_out_trade_no_returns_success(buyer_client, sku, db, monkeypatch, tmp_path):
    """微信侧同样：验签解密成功但 out_trade_no 查无流水 → 回成功停止重发，不建单不推进。"""
    platform_key, api_key = _use_wechat(monkeypatch, tmp_path)
    order = _new_order(buyer_client, sku)
    body, headers = _wechat_body(platform_key, {
        "out_trade_no": "PAY-WX-NONE",
        "transaction_id": "WX-TXN-X",
        "trade_state": "SUCCESS",
        "amount": {"total": 10000, "currency": "CNY"},
    }, api_key)
    r = buyer_client.post(
        "/api/v1/payments/notify/wechat", content=body,
        headers={"content-type": "application/json", **headers},
    )
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.execute(
        select(Payment).where(Payment.pay_no == "PAY-WX-NONE")
    ).scalar_one_or_none() is None
    assert db.get(Order, order["id"]).current_node_key == "pending_payment"


def test_payment_callback_rate_limited(buyer_client, sku, monkeypatch, tmp_path):
    """支付回调入口在验签前就按 IP 限流：超过阈值直接 429，挡住伪造签名的 CPU 放大风暴。"""
    # 配置成微信，便于用「渠道不匹配」的廉价请求压测（不触发真正的 RSA 验签，
    # 但仍会先经过限流依赖）；验证的是「限流在验签之前、且按 IP 计」。
    _use_wechat(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "RATE_LIMIT_PAYMENT_CALLBACK_MAX", 3)
    url = "/api/v1/payments/notify/alipay"  # 与配置的 wechat 不匹配 → 404，但先过限流
    codes = [
        buyer_client.post(url, content=b"x").status_code
        for _ in range(4)
    ]
    # 前 3 次放行（404），第 4 次超阈值被限流（429）
    assert codes[:3] == [404, 404, 404], codes
    assert codes[3] == 429

