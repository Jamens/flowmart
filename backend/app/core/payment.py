"""支付渠道路由：可插拔的渠道适配器（由 PAYMENT_PROVIDER 配置驱动）。

为什么做成「接口 + 多实现」而不是在业务代码里 if/else 分支渠道：
- 三个渠道的**签名算法、回调格式、金额单位**全不一样，混进订单逻辑会让那段代码
  同时承担「业务规则」和「渠道协议」两种职责，改一个渠道要动业务代码；
- 演示环境（mock）必须与真实渠道走**同一条调用路径**，否则本地永远测不到真实路径的
  分支，上线等于第一次跑。

本模块刻意**不碰数据库**：它只负责「把我们的下单请求翻译成渠道协议」和「把渠道回调
验签后翻译回结构化结果」。写支付流水、推进订单状态是 api/payments.py 与订单服务的职责。

⚠️ 金额单位各自不同（微信按**分**、支付宝按**元**），转换只在本模块内做，
对外一律用 Decimal 元，避免调用方搞混单位导致少收/多收钱。
"""
from __future__ import annotations

import base64
import json
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.x509 import load_pem_x509_certificate

from app.core.config import settings


class PaymentError(Exception):
    """支付渠道调用失败（网络、渠道返回错误、配置不全）。"""


class PaymentVerifyError(PaymentError):
    """回调验签失败——可能是伪造的回调，绝不能据此推进订单。"""


@dataclass
class CallbackResult:
    """验签通过后的回调结果，字段与渠道无关。"""

    out_trade_no: str  # 我方支付流水号（payments.pay_no）
    provider_trade_no: str  # 渠道侧交易号，对账用
    amount: Decimal  # 元
    success: bool  # 渠道侧是否真的支付成功


class PaymentProvider(ABC):
    """渠道适配器接口。新增渠道只需实现这三个方法并注册到 build_payment_provider。"""

    name: str = ""

    @abstractmethod
    def create_payment(
        self, *, out_trade_no: str, amount: Decimal, subject: str, notify_url: str
    ) -> dict:
        """发起支付，返回交给前端拉起收银台所需的参数。"""

    @abstractmethod
    def parse_callback(self, *, headers: dict[str, str], body: bytes) -> CallbackResult:
        """验签并解析渠道回调。验签失败必须抛 PaymentVerifyError。"""

    def callback_success_response(self) -> tuple[str, str]:
        """回调成功时应回的内容类型与正文。渠道靠它判断「我们收到了」。"""
        return "application/json", '{"code":"SUCCESS"}'


class MockPaymentProvider(PaymentProvider):
    """演示/开发渠道：发起即视为成功，不接真实网关。

    它与真实渠道走同一条调用路径（前端同样调「发起支付」接口），所以本地联调能覆盖
    到真实的分支逻辑，只是省掉了「跳收银台」这一步。
    """

    name = "mock"

    def create_payment(self, *, out_trade_no, amount, subject, notify_url) -> dict:
        return {
            "channel": "mock",
            "mode": "instant",
            "out_trade_no": out_trade_no,
            "amount": f"{amount:.2f}",
        }

    def parse_callback(self, *, headers, body) -> CallbackResult:
        raise PaymentError("mock 渠道不接受真实回调（它是发起即成功的同步模拟）")


def _load_private_key(path: str):
    """读商户/应用私钥。文件不可读在这里报，而不是启动时——容器里证书常由卷挂载。"""
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise PaymentError(f"读取私钥失败（{path}）：{exc}") from exc
    return serialization.load_pem_private_key(data, password=None)


def _load_public_key(path: str):
    """读渠道公钥（支付宝用）。"""
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise PaymentError(f"读取支付宝公钥失败（{path}）：{exc}") from exc
    return serialization.load_pem_public_key(data)


def _load_cert_public_key(path: str):
    """读微信支付平台证书里的公钥（回调验签用）。"""
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise PaymentError(f"读取微信支付平台证书失败（{path}）：{exc}") from exc
    return load_pem_x509_certificate(data).public_key()


class WechatPayProvider(PaymentProvider):
    """微信支付 APIv3 —— Native（扫码）支付。

    为什么只做 Native：JSAPI/小程序支付需要用户的 openid，而 openid 只能由公众号或
    小程序授权流程换来。本项目没有微信身份体系，硬接 JSAPI 会凭空多出一整套 OAuth。
    Native 返回 code_url，前端生成二维码即可，不需要用户身份。
    """

    name = "wechat"
    GATEWAY = "https://api.mch.weixin.qq.com"

    def __init__(self) -> None:
        self._mch_key = _load_private_key(settings.WECHAT_PRIVATE_KEY_PATH)
        self._platform_key = _load_cert_public_key(settings.WECHAT_PLATFORM_CERT_PATH)

    # ---- 请求签名：APIv3 要求对「方法/路径/时间戳/随机串/报文主体」整体签名 ----
    def _authorization(self, method: str, url_path: str, body: str) -> str:
        ts = str(int(time.time()))
        nonce = uuid.uuid4().hex
        message = f"{method}\n{url_path}\n{ts}\n{nonce}\n{body}\n"
        signature = self._mch_key.sign(message.encode(), padding.PKCS1v15(), hashes.SHA256())
        return (
            f'WECHATPAY2-SHA256-RSA2048 '
            f'mchid="{settings.WECHAT_MCHID}",'
            f'nonce_str="{nonce}",'
            f'signature="{base64.b64encode(signature).decode()}",'
            f'timestamp="{ts}",'
            f'serial_no="{settings.WECHAT_MCH_CERT_SERIAL_NO}"'
        )

    def create_payment(self, *, out_trade_no, amount, subject, notify_url) -> dict:
        import httpx

        url_path = "/v3/pay/transactions/native"
        payload = {
            "appid": settings.WECHAT_APPID,
            "mchid": settings.WECHAT_MCHID,
            "description": subject[:127],
            "out_trade_no": out_trade_no,
            "notify_url": notify_url,
            "amount": {"total": int(amount * 100), "currency": "CNY"},  # 微信按分
        }
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        try:
            resp = httpx.post(
                self.GATEWAY + url_path,
                content=body,
                headers={
                    "Authorization": self._authorization("POST", url_path, body),
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "flowmart",
                },
                timeout=10,
            )
        except Exception as exc:  # 网络层异常不应以 500 形态泄漏到用户
            raise PaymentError(f"调用微信支付下单失败：{exc}") from exc
        if resp.status_code != 200:
            raise PaymentError(f"微信支付下单失败（HTTP {resp.status_code}）：{resp.text}")
        return {
            "channel": "wechat",
            "mode": "qr",
            "code_url": resp.json().get("code_url", ""),
            "out_trade_no": out_trade_no,
        }

    def parse_callback(self, *, headers, body) -> CallbackResult:
        # 验签头三个都不能少：缺了就说明不是微信支付发来的
        ts = headers.get("wechatpay-timestamp", "")
        nonce = headers.get("wechatpay-nonce", "")
        signature = headers.get("wechatpay-signature", "")
        if not (ts and nonce and signature):
            raise PaymentVerifyError("微信回调缺少验签头，疑似伪造请求")

        message = f"{ts}\n{nonce}\n{body.decode('utf-8')}\n"
        try:
            self._platform_key.verify(
                base64.b64decode(signature), message.encode(), padding.PKCS1v15(), hashes.SHA256()
            )
        except InvalidSignature as exc:
            raise PaymentVerifyError("微信回调验签失败，疑似伪造回调") from exc

        resource = json.loads(body).get("resource") or {}
        plain = json.loads(self._decrypt_resource(resource))
        total = Decimal(str(plain.get("amount", {}).get("total", 0))) / 100  # 分 → 元
        return CallbackResult(
            out_trade_no=plain.get("out_trade_no", ""),
            provider_trade_no=plain.get("transaction_id", ""),
            amount=total,
            success=plain.get("trade_state") == "SUCCESS",
        )

    def _decrypt_resource(self, resource: dict[str, Any]) -> bytes:
        """APIv3 回调的敏感字段是 AES-256-GCM 加密的，密钥即 APIv3 密钥。

        GCM 是**认证加密**：解不开就说明报文被改过或密钥不对，绝不能「解密失败还继续用
        里面的金额」。微信把 16 字节 authTag 拼在密文尾部，cryptography 的 GCM 模式
        要求 tag 单独传，故按尾部 16 字节切分。
        """
        try:
            raw = base64.b64decode(resource["ciphertext"])
            nonce = resource["nonce"].encode()
            aad = resource.get("associated_data", "").encode()
        except (KeyError, ValueError) as exc:
            raise PaymentVerifyError(f"微信回调 resource 字段不完整：{exc}") from exc
        cipher_text, tag = raw[:-16], raw[-16:]
        decryptor = Cipher(
            algorithms.AES(settings.WECHAT_API_V3_KEY.encode()), modes.GCM(nonce, tag)
        ).decryptor()
        decryptor.authenticate_additional_data(aad)
        return decryptor.update(cipher_text) + decryptor.finalize()

    def callback_success_response(self) -> tuple[str, str]:
        return "application/json", '{"code":"SUCCESS","message":"成功"}'


class AlipayProvider(PaymentProvider):
    """支付宝电脑网站支付（alipay.trade.page.pay）。

    前端拿到的是一个跳转 URL，用户到支付宝收银台付款，完成后支付宝同步跳回、
    并**异步 POST 通知**我们的 notify_url。只有异步通知才是可靠的到账依据
    （同步跳转可被用户伪造/拦截），所以推进订单只看异步回调。
    """

    name = "alipay"

    def __init__(self) -> None:
        self._app_key = _load_private_key(settings.ALIPAY_PRIVATE_KEY_PATH)
        self._alipay_key = _load_public_key(settings.ALIPAY_PUBLIC_KEY_PATH)

    @staticmethod
    def _sign_content(params: dict[str, Any]) -> str:
        """待签名串：按 key 升序、排除 sign/sign_type、空值不参与。"""
        return "&".join(
            f"{k}={v}"
            for k, v in sorted(params.items())
            if k not in ("sign", "sign_type") and v not in (None, "")
        )

    def _sign(self, params: dict[str, Any]) -> str:
        signature = self._app_key.sign(
            self._sign_content(params).encode(), padding.PKCS1v15(), hashes.SHA256()
        )
        return base64.b64encode(signature).decode()

    def create_payment(self, *, out_trade_no, amount, subject, notify_url) -> dict:
        biz = {
            "out_trade_no": out_trade_no,
            "total_amount": f"{amount:.2f}",  # 支付宝按元、两位小数字符串
            "subject": subject[:256],
            "product_code": "FAST_INSTANT_TRADE_PAY",
        }
        params = {
            "app_id": settings.ALIPAY_APPID,
            "method": "alipay.trade.page.pay",
            "format": "JSON",
            "charset": "utf-8",
            "sign_type": settings.ALIPAY_SIGN_TYPE,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "version": "1.0",
            "notify_url": notify_url,
            "biz_content": json.dumps(biz, separators=(",", ":"), ensure_ascii=False),
        }
        params["sign"] = self._sign(params)
        return {
            "channel": "alipay",
            "mode": "redirect",
            "pay_url": f"{settings.ALIPAY_GATEWAY}?{urlencode(params)}",
            "out_trade_no": out_trade_no,
        }

    def parse_callback(self, *, headers, body) -> CallbackResult:
        # 回调是 application/x-www-form-urlencoded；parse_qsl 已做 URL 解码，
        # 而支付宝要求用**解码后**的值拼待签名串（用编码值验签必然失败）
        params = dict(parse_qsl(body.decode("utf-8")))
        signature = params.get("sign", "")
        if not signature:
            raise PaymentVerifyError("支付宝回调缺少 sign，疑似伪造请求")
        try:
            self._alipay_key.verify(
                base64.b64decode(signature),
                self._sign_content(params).encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except InvalidSignature as exc:
            raise PaymentVerifyError("支付宝回调验签失败，疑似伪造回调") from exc

        return CallbackResult(
            out_trade_no=params.get("out_trade_no", ""),
            provider_trade_no=params.get("trade_no", ""),
            amount=Decimal(params.get("total_amount") or "0"),
            success=params.get("trade_status") in ("TRADE_SUCCESS", "TRADE_FINISHED"),
        )

    def callback_success_response(self) -> tuple[str, str]:
        # 支付宝只认这个单词，返回别的它会持续重发通知
        return "text/plain", "success"


_PROVIDER: PaymentProvider | None = None


def build_payment_provider() -> PaymentProvider:
    """按配置构造渠道。凭据不全的问题在 config 的启动校验里已拦过一道。"""
    name = settings.PAYMENT_PROVIDER
    if name == "wechat":
        return WechatPayProvider()
    if name == "alipay":
        return AlipayProvider()
    return MockPaymentProvider()


def get_payment_provider() -> PaymentProvider:
    """进程内单例：密钥只在首次使用时读一次，避免每次下单都读盘。"""
    global _PROVIDER
    if _PROVIDER is None:
        _PROVIDER = build_payment_provider()
    return _PROVIDER


def reset_payment_provider() -> None:
    """丢弃单例（测试切换渠道用；生产不需要调）。"""
    global _PROVIDER
    _PROVIDER = None
