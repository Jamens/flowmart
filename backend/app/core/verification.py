"""邮箱 / 手机验证码（OTP）核心逻辑。

设计：
- 两步式：先 `request_code` 申请（生成并发送一次性口令），再 `confirm_code` 确认。
- 确认成功后把 target 绑定到账号并置对应渠道的 `*_verified=True`。
- 发送器可插拔且**配置驱动**（本项目开源，不替使用者选厂商）：
    console —— 默认，打印到日志，零凭据即可跑通注册流程；
    smtp    —— 邮件，标准库 smtplib，任何 SMTP 服务商都能连，零新增依赖；
    webhook —— 短信，通用 HTTP 网关，报文模板由部署者自己填，不绑定任何一家厂商 SDK。
  邮件与短信各自独立选择（OTP_EMAIL_SENDER / OTP_SMS_SENDER），配错在启动时报错。
- 重发限流基于 DB（同一用户对同一渠道在时间窗内最多 N 次），天然多实例安全，
  不依赖进程内内存或 Redis。
- 旧码作废：每次申请都会把同用户同渠道同 target 的未消费旧码置为已消费，防重放。
- 发送失败要回滚：发不出去的码不该占用重发配额，否则渠道一挂用户就被锁死在限流里。
"""
import hmac
import json
import re
import secrets
import smtplib
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Literal
from urllib.parse import parse_qsl

import httpx
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
    """发送器抽象：把验证码投递到用户的邮箱 / 手机。

    契约：发送失败一律抛 VerificationError（带建议状态码），调用方不需要认识
    smtplib / httpx 的异常类型；成功则静默返回。
    """

    @abstractmethod
    def send(self, channel: str, target: str, code: str, ttl_seconds: int) -> None:
        ...


class ConsoleSender(VerificationSender):
    """零凭据默认发送器：仅打印到日志（stdout）。

    默认就用它是刻意的——开源项目 clone 下来不该被「先去申请短信服务」卡住。
    生产环境不允许落到 console（见 config._require_notification_settings）。
    """

    def send(self, channel: str, target: str, code: str, ttl_seconds: int) -> None:
        print(f"[verification] 向 {target} 发送{channel}验证码：{code}（{ttl_seconds} 秒内有效）")


class SmtpSender(VerificationSender):
    """邮件发送器：标准库 smtplib，**零新增依赖**，可连任意 SMTP 服务商。

    每次发送新建连接（而非长连接池）：发验证码是低频操作（有重发限流兜着），
    维持长连接反而要处理空闲超时、服务端断连重连等问题，不划算。
    """

    def send(self, channel: str, target: str, code: str, ttl_seconds: int) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"【{settings.PROJECT_NAME}】您的验证码：{code}"
        msg["From"] = settings.SMTP_FROM.strip() or settings.SMTP_USER
        msg["To"] = target
        msg.set_content(
            f"您的验证码是 {code}，{ttl_seconds} 秒内有效。\n\n"
            "若非本人操作，请忽略本邮件；请勿将验证码告知任何人。"
        )
        # 465 用 SSL 直连，587 用明文连上再 STARTTLS 升级
        factory = smtplib.SMTP_SSL if settings.SMTP_USE_SSL else smtplib.SMTP
        try:
            with factory(
                settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT
            ) as server:
                if settings.SMTP_STARTTLS and not settings.SMTP_USE_SSL:
                    server.starttls()
                if settings.SMTP_USER:
                    server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                server.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            # 超时 / 连不上 / 认证失败都算上游故障：502 让前端可重试，
            # 不把第三方异常原样抛出去（既难排查也泄漏内部细节）。
            raise VerificationError("验证码发送失败，请稍后再试", status_code=502) from exc


class WebhookSmsSender(VerificationSender):
    """短信发送器：通用 HTTP 网关，**刻意不内置任何一家厂商的 SDK**。

    各家签名算法不同且会变，内置 SDK 等于替使用者选厂商，还要把他们的包变成
    本项目的运行时依赖。这里只做一件事：把模板里的占位符替换掉，按配置的方法发出去。
    网关可以是厂商地址，也可以是自建的一小段转发服务（想接谁就接谁）。

    模板占位符：{target} 收信号码、{code} 验证码、{ttl} 有效秒数。
    POST 时整体作为请求体（通常配成 JSON）；GET 时按 query string 解析成查询参数，
    此时模板应写成 `to={target}&text=...` 形式。
    """

    def send(self, channel: str, target: str, code: str, ttl_seconds: int) -> None:
        body = (
            settings.SMS_WEBHOOK_BODY.replace("{target}", target)
            .replace("{code}", code)
            .replace("{ttl}", str(ttl_seconds))
        )
        headers = {"Content-Type": "application/json"}
        # 头部 JSON 已在配置校验里解析过一次（fail-fast），这里不会再抛
        if settings.SMS_WEBHOOK_HEADERS.strip():
            headers.update(json.loads(settings.SMS_WEBHOOK_HEADERS))

        kwargs = {"timeout": settings.SMS_WEBHOOK_TIMEOUT, "headers": headers}
        if settings.SMS_WEBHOOK_METHOD == "GET":
            # keep_blank_values：值为空的字段（如签名位）不能被静默丢掉
            kwargs["params"] = dict(parse_qsl(body, keep_blank_values=True))
        else:
            kwargs["content"] = body.encode("utf-8")

        try:
            resp = httpx.request(settings.SMS_WEBHOOK_METHOD, settings.SMS_WEBHOOK_URL, **kwargs)
        except httpx.HTTPError as exc:
            # 超时 / DNS 失败 / 连接被拒：上游故障，502 可重试
            raise VerificationError("验证码发送失败，请稍后再试", status_code=502) from exc
        if not (settings.SMS_SUCCESS_MIN_STATUS <= resp.status_code <= settings.SMS_SUCCESS_MAX_STATUS):
            # 网关返回了但说失败：同样转 502，绝不能让接口报 200 说「已发送」
            raise VerificationError("验证码发送失败，请稍后再试", status_code=502)


def _sender_kind(channel: str) -> str:
    """某渠道最终使用的发送器名：单渠道覆盖优先，否则跟随全局。"""
    if channel == "email":
        return settings.OTP_EMAIL_SENDER or settings.OTP_SENDER
    return settings.OTP_SMS_SENDER or settings.OTP_SENDER


def build_sender(channel: str) -> VerificationSender:
    """按当前配置为某渠道构建发送器（取值合法性由 config 校验保证）。"""
    kind = _sender_kind(channel)
    if kind == "smtp":
        return SmtpSender()
    if kind == "webhook":
        return WebhookSmsSender()
    return ConsoleSender()


# 进程内单例：配置在启动时已校验，这里只构建一次；reset_senders() 供测试切换渠道用
_SENDERS: dict[str, VerificationSender] | None = None


def get_sender(channel: str) -> VerificationSender:
    """取该渠道的发送器（首次调用时构建）。"""
    global _SENDERS
    if _SENDERS is None:
        _SENDERS = {ch: build_sender(ch) for ch in ("email", "phone")}
    return _SENDERS[channel]


def reset_senders() -> None:
    """丢弃发送器单例（测试切换渠道后必须调，否则新配置不生效）。"""
    global _SENDERS
    _SENDERS = None


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

    # 先发送、后落库：发出去的码才算数。渠道故障时整段回滚，否则这条用户根本没收到的
    # 码会占掉重发配额——上游一挂用户就被自己的限流锁死，且现象是「点了没反应」。
    try:
        get_sender(channel).send(channel, target, code, settings.OTP_TTL_SECONDS)
    except VerificationError:
        db.rollback()
        raise
    except Exception as exc:
        # 兜底网：各发送器已把已知的上游异常转成 VerificationError，这里拦的是漏网的
        # （新增发送器忘了转换、或第三方抛了没预料到的类型）——统一 502 + 回滚，
        # 绝不让第三方异常原样变成 500 堆栈（既难排查也泄漏内部细节）。
        db.rollback()
        raise VerificationError("验证码发送失败，请稍后再试", status_code=502) from exc

    db.commit()
    db.refresh(vc)
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
