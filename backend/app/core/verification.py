"""邮箱 / 手机验证码（OTP）核心逻辑。

设计：
- 两步式：先 `request_code` 申请（生成并发送一次性口令），再 `confirm_code` 确认。
- 确认成功后把 target 绑定到账号并置对应渠道的 `*_verified=True`。
- 发送器可插拔：`ConsoleSender` 已实现（开发期 print 到日志）；smtp / sms 是预留扩展点，
  未实现时配置了会启动报错（fail-fast）。
- 重发限流基于 DB（同一用户对同一渠道在时间窗内最多 N 次），天然多实例安全，
  不依赖进程内内存或 Redis。
- 旧码作废：每次申请都会把同用户同渠道同 target 的未消费旧码置为已消费，防重放。
"""
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Literal

import hmac
import re
import secrets

from sqlalchemy import func, select

from app.core.config import settings
from app.models.ecommerce import User, VerificationCode

Channel = Literal["email", "phone"]

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# + 前缀最多再带 19 位数字，总长度 <= 20，与 User.phone 的 String(20) 上限一致（MySQL strict 下超限会写失败）
_PHONE_RE = re.compile(r"^\+?\d{6,19}$")


class VerificationError(Exception):
    """验证码业务的统一异常，携带建议的 HTTP 状态码。"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class VerificationSender(ABC):
    """发送器抽象：把验证码投递到用户的邮箱 / 手机。"""

    @abstractmethod
    def send(self, channel: str, target: str, code: str, ttl_seconds: int) -> None:
        ...


class ConsoleSender(VerificationSender):
    """开发用发送器：仅打印到日志（stdout）。生产应替换为真实 SMTP / SMS。"""

    def send(self, channel: str, target: str, code: str, ttl_seconds: int) -> None:
        print(f"[verification] 向 {target} 发送{channel}验证码：{code}（{ttl_seconds} 秒内有效）")


def _build_sender() -> VerificationSender:
    name = settings.OTP_SENDER
    if name == "console":
        return ConsoleSender()
    # smtp / sms 是预留扩展点，未实现时 fail-fast，而不是静默不发送（那比报错更危险）
    raise ValueError(
        f"不支持的 OTP_SENDER={name!r}：当前仅实现 console，smtp/sms 为未实现扩展点"
    )


# 发送器单例：随配置在导入时构建；配置非法会直接让应用启动失败
sender = _build_sender()


def generate_otp(length: int) -> str:
    """生成 length 位纯数字验证码，用 secrets 保证密码学随机性。"""
    return "".join(secrets.choice("0123456789") for _ in range(length))


def _validate_target(channel: str, target: str) -> None:
    if channel == "email" and not _EMAIL_RE.match(target):
        raise VerificationError("邮箱格式不正确")
    if channel == "phone" and not _PHONE_RE.match(target):
        raise VerificationError("手机号格式不正确")


def request_code(db, user: User, channel: str, target: str, purpose: str = "verify") -> str:
    """申请验证码：校验 target 格式 → 重发限流 → 作废旧码 → 生成并发送。

    purpose 区分验证码用途（默认 "verify" 验证联系方式；"reset" 用于找回密码），
    不同用途的码互不通用，confirm_code 会按 purpose 过滤，避免被跨用途复用。

    返回明文 code，仅用于开发环境（OTP_DEV_RETURN_CODE=True）由接口回传；
    生产环境该开关必须为 False，调用方不应依赖返回值。
    """
    if channel not in ("email", "phone"):
        raise VerificationError("channel 必须为 email 或 phone")
    _validate_target(channel, target)

    now = datetime.now()
    # 重发限流：时间窗内同一用户同一渠道的发送次数
    window_start = now - timedelta(seconds=settings.OTP_RESEND_WINDOW)
    recent = db.scalar(
        select(func.count(VerificationCode.id)).where(
            VerificationCode.user_id == user.id,
            VerificationCode.channel == channel,
            VerificationCode.created_at >= window_start,
        )
    )
    if recent and recent >= settings.OTP_MAX_PER_WINDOW:
        raise VerificationError("验证码发送过于频繁，请稍后再试", status_code=429)

    # 作废同用户同渠道同 target 的未消费旧码，避免多码并存被重放
    stale = db.execute(
        select(VerificationCode).where(
            VerificationCode.user_id == user.id,
            VerificationCode.channel == channel,
            VerificationCode.target == target,
            VerificationCode.consumed_at.is_(None),
        )
    ).scalars().all()
    for old in stale:
        old.consumed_at = now

    code = generate_otp(settings.OTP_LENGTH)
    vc = VerificationCode(
        user_id=user.id,
        channel=channel,
        target=target,
        code=code,
        purpose=purpose,
        # 显式用 Python 时钟写 created_at，保证与下方重发限流窗口（同用 datetime.now()）时钟基准一致；
        # 否则 DB 的 server_default func.now() 是 UTC，与本地时钟比较会恒为假，限流失效。
        created_at=now,
        expires_at=now + timedelta(seconds=settings.OTP_TTL_SECONDS),
    )
    db.add(vc)
    db.commit()
    db.refresh(vc)

    sender.send(channel, target, code, settings.OTP_TTL_SECONDS)
    return code


def confirm_code(db, user: User, channel: str, target: str, code: str, purpose: str = "verify") -> None:
    """确认验证码：取最新一条未消费且未过期、且 purpose 匹配的码，常量时间比对后置位验证状态。

    purpose 必须与 request_code 时一致（默认 "verify"），否则视为无效 —— 确保找回密码用的
    reset 码不能被拿去当验证联系方式用，反之亦然。
    """
    if channel not in ("email", "phone"):
        raise VerificationError("channel 必须为 email 或 phone")
    now = datetime.now()

    vc = db.execute(
        select(VerificationCode)
        .where(
            VerificationCode.user_id == user.id,
            VerificationCode.channel == channel,
            VerificationCode.target == target,
            VerificationCode.purpose == purpose,
            VerificationCode.consumed_at.is_(None),
            VerificationCode.expires_at > now,
        )
        .order_by(VerificationCode.created_at.desc())
    ).scalars().first()

    if vc is None:
        raise VerificationError("验证码无效或已过期")
    # 常量时间比较，避免时序侧信道泄露正确位数
    if not hmac.compare_digest(vc.code, code):
        vc.attempts += 1
        # 超过确认尝试上限即锁定该码（置为已消费），杜绝对 6 位码的暴力枚举
        if vc.attempts >= settings.OTP_CONFIRM_MAX_ATTEMPTS:
            vc.consumed_at = now
        db.commit()
        raise VerificationError("验证码错误")

    vc.consumed_at = now
    # 只有「验证联系方式」用途才绑定联系方式并置位 verified。
    # 找回密码（purpose="reset"）若也置位，会让一次改密悄悄撤销掉管理员此前的「撤销验证」——
    # 被撤销验证的账号本应被登录闸门挡住，凭改密就能重新登录等于把闸门绕过去了。
    if purpose == "verify":
        if channel == "email":
            user.email = target
            user.email_verified = True
        else:  # phone
            user.phone = target
            user.phone_verified = True
    db.commit()
