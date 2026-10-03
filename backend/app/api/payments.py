"""支付接口：发起支付 + 渠道异步回调。

这里是全项目最容易出资金安全事故的地方，三条铁律贯穿下面每一行代码：

1. **`pay` 事件绝不进 `BUYER_ALLOWED_EVENTS`**。
   那等于允许买家自己把订单标记成「已付款」而不真付钱（0 元提货）。推进订单只能由
   **渠道回调**以 system 身份触发——渠道说钱到了，我们才认。
2. **回调必须验签后才可信**。未验签就采信回调，等于任何人 POST 一下就能把订单改成
   已支付。验签在 core/payment.py 的 parse_callback 里完成，失败直接 400。
3. **回调必须幂等**。渠道收不到成功响应会反复重发（微信按 15s/15s/30s/... 阶梯重发
   数小时），不幂等就会重复推进订单、重复记流水。

「订单当前能不能支付」不问硬编码的状态名，而是问引擎的 `available_events`——
流程定义改了（比如加一道审批）这里不用跟着改。
"""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.payment import PaymentError, PaymentVerifyError, get_payment_provider
from app.core.security import get_current_user
from app.models.ecommerce import Order, Payment
from app.services.order_service import OrderService, _gen_no

router = APIRouter(tags=["payments"])

# 回调里推进订单时用的操作人标识。刻意带 `system:` 前缀：审计时间线里一眼能看出
# 这笔是渠道回调推的，而不是某个管理员手点的。
CALLBACK_OPERATOR = "system:payment-callback"


def _notify_url(channel: str) -> str:
    """渠道回调地址。基址来自配置（必须是渠道能访问到的公网 HTTPS 地址）。"""
    return f"{settings.PAYMENT_NOTIFY_BASE_URL}{settings.API_V1_PREFIX}/payments/notify/{channel}"


def _available_events(svc: OrderService, order: Order) -> set[str]:
    """当前可执行事件名集合（兼容 dict 与字符串两种返回形态）。"""
    names: set[str] = set()
    for item in svc.available_events(order):
        if isinstance(item, dict):
            name = item.get("event") or item.get("name")
        else:
            name = item
        if name:
            names.add(str(name))
    return names


@router.post("/orders/{order_id}/payments")
def create_payment(
    order_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """为订单发起支付。

    - mock 渠道：发起即成功，直接推进（演示/联调保持一条路径，可离线跑通）
    - 真实渠道：先落一条 pending 流水，再把渠道要求的参数返回给前端拉起收银台
    """
    order = db.get(Order, order_id)
    # 他人订单与「不存在」同等处理：统一 404，不泄露订单是否存在
    if order is None or order.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="订单不存在")

    svc = OrderService(db)
    if "pay" not in _available_events(svc, order):
        raise HTTPException(status_code=400, detail="订单当前状态不可支付")

    provider = get_payment_provider()

    # 幂等：已有 pending 流水就复用它的 pay_no。若每次都新生成，渠道会报
    # 「商户订单号重复」或产生两条待支付记录，对账时说不清哪条是真的。
    payment = (
        db.execute(
            select(Payment).where(Payment.order_id == order.id, Payment.status == "pending")
        )
        .scalars()
        .first()
    )
    if payment is None:
        payment = Payment(
            order_id=order.id,
            pay_no=_gen_no("PAY"),
            amount=order.pay_amount,
            channel=provider.name,
            status="pending",
        )
        db.add(payment)
        db.flush()

    if provider.name == "mock":
        # mock 直接推进：与真实路径共用「先落 pending 再置成功」，保证联调能覆盖
        # 到真实分支，而不是走一条只在 mock 下存在的捷径。
        svc.trigger(order.id, "pay", operator=f"user:{current_user.id}", comment="mock 支付")
        return {"channel": "mock", "paid": True, "pay_no": payment.pay_no}

    try:
        params = provider.create_payment(
            out_trade_no=payment.pay_no,
            amount=Decimal(str(order.pay_amount)),
            subject=f"订单 {order.order_no}",
            notify_url=_notify_url(provider.name),
        )
    except PaymentError as exc:
        # 渠道下单失败：撤掉刚落的 pending 流水，下次发起重新生成单号，
        # 避免留下一堆没人认领的待支付记录。
        db.rollback()
        raise HTTPException(status_code=502, detail=f"发起支付失败：{exc}") from exc

    db.commit()
    return params


@router.post("/payments/notify/{channel}")
async def payment_notify(
    channel: str, request: Request, db: Session = Depends(get_db)
):
    """渠道异步回调：验签 → 落流水 → 推进订单。

    无鉴权（渠道不会有我们的令牌），安全性完全依赖**验签**。用 async 是为了拿到
    原始报文体（签名是对原始字节做的，任何重新序列化都会破坏验签）。

    ⚠️ 会话必须走 `Depends(get_db)`，**不要**自己 `SessionLocal()`：
    后者连的是全局 engine（生产库），会绕过测试注入的会话，导致测试打到真实库上
    ——轻则用例红，重则把测试脏数据写进生产库。走依赖注入才能被 conftest 的
    `dependency_overrides` 接管。
    """
    provider = get_payment_provider()
    # 渠道与当前配置不符就不处理：避免把微信的回调喂给支付宝的验签逻辑
    if provider.name != channel:
        raise HTTPException(status_code=404, detail="not found")

    body = await request.body()
    try:
        result = provider.parse_callback(headers=dict(request.headers), body=body)
    except PaymentVerifyError as exc:
        # 验签失败绝不推进订单；也不回成功，让渠道重试（便于排查且不给伪造者可乘之机）
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PaymentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    media_type, ok_text = provider.callback_success_response()
    if not result.success:
        # 渠道来了「用户还没付完」这类非成功通知：回成功**停止重发**，但不碰订单。
        # 不回成功的话渠道会一直重发到阶梯上限，白白放大这几小时的请求量。
        return Response(content=ok_text, media_type=media_type)

    payment = (
        db.execute(select(Payment).where(Payment.pay_no == result.out_trade_no))
        .scalars()
        .first()
    )
    if payment is None:
        # 找不到对应流水：可能是伪造回调（验签过了但单号是编的），
        # 也可能是我们这边流水被清了。无论哪种都不能凭空建单，直接报错。
        raise HTTPException(status_code=404, detail="支付流水不存在")

    if payment.status == "success":
        # 幂等：重复回调直接回成功，不再推进一次订单
        return Response(content=ok_text, media_type=media_type)

    # 金额校验：回调里的金额必须与待支付金额一致。少了说明被篡改或优惠被绕过，
    # 多了说明渠道侧配置异常——两种都不能默默放行。
    if result.amount != Decimal(str(payment.amount)):
        raise HTTPException(
            status_code=400,
            detail=f"回调金额与订单金额不符（回调 {result.amount}，应收 {payment.amount}）",
        )

    payment.status = "success"
    payment.provider_trade_no = result.provider_trade_no
    payment.paid_at = payment.paid_at or _now()
    db.commit()

    # 到这里钱确实到了，才推进订单。用 system 身份而非买家身份——
    # 审计轨迹要能区分「用户自己点的」和「渠道回调推的」。
    order = db.get(Order, payment.order_id)
    svc = OrderService(db)
    try:
        svc.trigger(
            order.id, "pay", operator=CALLBACK_OPERATOR,
            comment=f"渠道回调 {result.provider_trade_no}",
        )
    except Exception:  # noqa: BLE001
        # 推进失败（例如订单已被超时取消）：**钱已经收了**，必须回成功让渠道停止
        # 重发——回失败只会让它重发几小时，而订单状态并不会因此变好。
        # 流水已落库（含渠道交易号），这种「已收款未推进」由人工按 provider_trade_no
        # 对账处理（退款或人工推进）。这里刻意不回滚流水：钱是真的到账了。
        db.rollback()

    return Response(content=ok_text, media_type=media_type)


def _now():
    from datetime import datetime

    return datetime.now()
