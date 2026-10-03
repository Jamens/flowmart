"""通知渠道（邮件 / 短信）可配置发送器的测试。

本项目是开源的，通知这块有两条不能破的底线，各自都用一条用例锁死：
1. **零凭据可跑**：默认 console，clone 下来不必先申请任何服务商就能走通注册 / 验证。
2. **不绑厂商**：短信走通用 HTTP 网关（模板 + 占位符），不内置任何一家 SDK。
"""
import json
import smtplib

import httpx
import pytest
from sqlalchemy import func, select

from app.core import verification
from app.core.config import Settings
from app.core.security import hash_password
from app.core.verification import (
    ConsoleSender,
    SmtpSender,
    VerificationError,
    WebhookSmsSender,
    request_code,
)
from app.models.ecommerce import User, VerificationCode


@pytest.fixture(autouse=True)
def _reset_senders():
    """发送器是模块级单例，用例改完配置必须丢弃，否则串到下一个用例。"""
    yield
    verification.reset_senders()


@pytest.fixture
def use(monkeypatch):
    """改写通知配置。必须用 monkeypatch：settings 是全进程单例，
    直接赋值会把当前用例的配置留给后面所有测试文件（跨文件的隐形污染）。"""

    def _set(**kw):
        for k, v in kw.items():
            monkeypatch.setattr(verification.settings, k, v)

    return _set


# --------------------------------------------------------------------------- #
# 发送器选择
# --------------------------------------------------------------------------- #
def test_default_is_console_zero_credential():
    """默认必须是 console：开源项目不该让第一次 clone 的人先去申请短信服务。"""
    assert isinstance(verification.get_sender("email"), ConsoleSender)
    assert isinstance(verification.get_sender("phone"), ConsoleSender)


def test_email_and_phone_choose_independently(use):
    """邮件走 SMTP、短信走网关这种混合配置必须支持（否则没法一半接一半不接）。"""
    use(OTP_SENDER="console", OTP_EMAIL_SENDER="smtp", OTP_SMS_SENDER="webhook")
    assert isinstance(verification.get_sender("email"), SmtpSender)
    assert isinstance(verification.get_sender("phone"), WebhookSmsSender)


def test_channel_override_falls_back_to_global(use):
    """单渠道留空时跟随全局 OTP_SENDER。"""
    use(OTP_SENDER="smtp", OTP_EMAIL_SENDER="", OTP_SMS_SENDER="")
    assert isinstance(verification.build_sender("email"), SmtpSender)
    assert isinstance(verification.build_sender("phone"), SmtpSender)


# --------------------------------------------------------------------------- #
# SMTP
# --------------------------------------------------------------------------- #
class _FakeSMTP:
    """替换 smtplib.SMTP：记录调用而不真的连外网。"""

    calls: list = []

    def __init__(self, host, port, timeout=None):
        _FakeSMTP.calls.append(("init", host, port, timeout))
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        _FakeSMTP.calls.append(("starttls",))

    def login(self, user, pwd):
        _FakeSMTP.calls.append(("login", user, pwd))

    def send_message(self, msg):
        _FakeSMTP.calls.append(("send", msg))


@pytest.fixture
def fake_smtp(monkeypatch):
    _FakeSMTP.calls = []
    monkeypatch.setattr(verification.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(verification.smtplib, "SMTP_SSL", _FakeSMTP)
    return _FakeSMTP


def test_smtp_sends_mail_with_code(use, fake_smtp):
    """STARTTLS + 登录 + 邮件正文里带验证码。"""
    use(
        OTP_SENDER="console",
        OTP_EMAIL_SENDER="smtp",
        SMTP_HOST="smtp.example.com",
        SMTP_PORT=587,
        SMTP_USER="noreply",
        SMTP_PASSWORD="secret",
        SMTP_FROM="no-reply@example.com",
        SMTP_STARTTLS=True,
        SMTP_USE_SSL=False,
        SMTP_TIMEOUT=7,
    )
    verification.get_sender("email").send("email", "user@example.com", "123456", 600)

    kinds = [c[0] for c in fake_smtp.calls]
    assert kinds == ["init", "starttls", "login", "send"]
    assert fake_smtp.calls[0][1:] == ("smtp.example.com", 587, 7)  # 超时必须传下去
    msg = fake_smtp.calls[-1][1]
    assert msg["To"] == "user@example.com"
    assert msg["From"] == "no-reply@example.com"
    assert "123456" in msg["Subject"]
    assert "123456" in msg.get_content()


def test_smtp_ssl_skips_starttls(use, fake_smtp):
    """465 端口是 SSL 直连，再调 starttls 会报错（已加密通道上二次协商）。"""
    use(
        OTP_SENDER="console",
        OTP_EMAIL_SENDER="smtp",
        SMTP_HOST="smtp.example.com",
        SMTP_PORT=465,
        SMTP_USER="noreply",
        SMTP_PASSWORD="secret",
        SMTP_FROM="",
        SMTP_USE_SSL=True,
        SMTP_STARTTLS=True,  # 与 SSL 互斥，SSL 优先
    )
    verification.get_sender("email").send("email", "user@example.com", "123456", 600)

    kinds = [c[0] for c in fake_smtp.calls]
    assert "starttls" not in kinds
    # SMTP_FROM 留空时回退到 SMTP_USER，避免发出没有发件人的信（会被直接拒收）
    assert fake_smtp.calls[-1][1]["From"] == "noreply"


def test_smtp_failure_becomes_502(use, monkeypatch):
    """SMTP 连不上 / 超时 / 认证失败：502 可重试，不能让第三方异常原样抛成 500。"""
    use(
        OTP_SENDER="console",
        OTP_EMAIL_SENDER="smtp",
        SMTP_HOST="smtp.example.com",
        SMTP_PORT=587,
        SMTP_USER="noreply",
        SMTP_PASSWORD="secret",
        SMTP_FROM="no-reply@example.com",
        SMTP_USE_SSL=False,
        SMTP_STARTTLS=True,
    )

    def _boom(*a, **kw):
        raise smtplib.SMTPConnectError(421, "service not available")

    monkeypatch.setattr(verification.smtplib, "SMTP", _boom)
    with pytest.raises(VerificationError) as ei:
        verification.get_sender("email").send("email", "user@example.com", "123456", 600)
    assert ei.value.status_code == 502


# --------------------------------------------------------------------------- #
# 短信 webhook
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


@pytest.fixture
def fake_http(monkeypatch):
    captured = {}

    def _request(method, url, **kw):
        status = captured.pop("__status__", 200)
        captured.update(method=method, url=url, **kw)
        return _Resp(status)

    monkeypatch.setattr(verification.httpx, "request", _request)
    return captured


def test_webhook_renders_placeholders(use, fake_http):
    """模板里三个占位符都要替换掉，否则网关收到的是字面量 {code}。"""
    use(
        OTP_SENDER="console",
        OTP_SMS_SENDER="webhook",
        SMS_WEBHOOK_URL="https://sms.example.com/send",
        SMS_WEBHOOK_METHOD="POST",
        SMS_WEBHOOK_BODY='{"to":"{target}","text":"验证码{code}({ttl}s)"}',
        SMS_WEBHOOK_HEADERS='{"Authorization":"Bearer abc"}',
        SMS_WEBHOOK_TIMEOUT=5,
    )
    verification.get_sender("phone").send("phone", "13800000000", "654321", 300)

    assert fake_http["method"] == "POST"
    assert fake_http["url"] == "https://sms.example.com/send"
    assert json.loads(fake_http["content"].decode()) == {
        "to": "13800000000",
        "text": "验证码654321(300s)",
    }
    assert fake_http["headers"]["Authorization"] == "Bearer abc"
    assert fake_http["timeout"] == 5


def test_webhook_get_sends_query_params(use, fake_http):
    """GET 网关：模板按 query string 解析成查询参数（此时模板写成 k=v 形式）。"""
    use(
        OTP_SENDER="console",
        OTP_SMS_SENDER="webhook",
        SMS_WEBHOOK_URL="https://sms.example.com/send",
        SMS_WEBHOOK_METHOD="GET",
        SMS_WEBHOOK_BODY="to={target}&text=code{code}",
        SMS_WEBHOOK_HEADERS="",
    )
    verification.get_sender("phone").send("phone", "13800000000", "111222", 60)

    assert fake_http["method"] == "GET"
    assert fake_http["params"] == {"to": "13800000000", "text": "code111222"}
    assert "content" not in fake_http


def test_webhook_non_2xx_becomes_502(use, fake_http):
    """网关返回失败要转成 502（可重试），不能让接口报 200 说「已发送」。"""
    fake_http["__status__"] = 500
    use(
        OTP_SENDER="console",
        OTP_SMS_SENDER="webhook",
        SMS_WEBHOOK_URL="https://sms.example.com/send",
        SMS_WEBHOOK_METHOD="POST",
        SMS_WEBHOOK_BODY='{"to":"{target}","code":"{code}"}',
        SMS_WEBHOOK_HEADERS="",
    )
    with pytest.raises(VerificationError) as ei:
        verification.get_sender("phone").send("phone", "13800000000", "111222", 60)
    assert ei.value.status_code == 502


def test_webhook_upstream_error_becomes_502(use, monkeypatch):
    """网关超时 / 连不上属于上游故障：502 + 友好提示，不要把第三方异常抛成 500。"""
    use(
        OTP_SENDER="console",
        OTP_SMS_SENDER="webhook",
        SMS_WEBHOOK_URL="https://sms.example.com/send",
        SMS_WEBHOOK_METHOD="POST",
        SMS_WEBHOOK_BODY='{"code":"{code}"}',
        SMS_WEBHOOK_HEADERS="",
    )

    def _boom(*a, **kw):
        raise httpx.TimeoutException("upstream dead")

    monkeypatch.setattr(verification.httpx, "request", _boom)
    with pytest.raises(VerificationError) as ei:
        verification.get_sender("phone").send("phone", "13800000000", "111222", 60)
    assert ei.value.status_code == 502


# --------------------------------------------------------------------------- #
# 发送失败与重发配额
# --------------------------------------------------------------------------- #
def test_send_failure_rolls_back_and_keeps_quota(db, monkeypatch):
    """渠道挂掉时不能把码落库：否则这条用户没收到的码会占掉重发配额，用户被自己限流锁死。"""
    u = User(username="smsuser", phone="13800000001", email="s@example.com",
             password_hash=hash_password("123456"))
    db.add(u)
    db.commit()
    db.refresh(u)

    class _Boom(verification.VerificationSender):
        def send(self, *a, **kw):
            raise RuntimeError("gateway down")

    monkeypatch.setattr(verification, "get_sender", lambda ch: _Boom())
    with pytest.raises(VerificationError) as ei:
        request_code(db, u, "phone", "13800000001")
    assert ei.value.status_code == 502

    # 没有留下任何码 —— 重发配额未被消耗
    assert db.scalar(select(func.count(VerificationCode.id))) == 0

    # 渠道恢复后立刻能再发，不会被限流挡住
    monkeypatch.undo()
    code = request_code(db, u, "phone", "13800000001")
    assert len(code) == 6


# --------------------------------------------------------------------------- #
# 配置守卫（fail-fast）
# --------------------------------------------------------------------------- #
def _settings(**kw):
    """构造 Settings 时**不读 .env**（_env_file=None）。

    用例的输入必须完全由 monkeypatch 决定：本机 .env 里可能放着端到端验收用的
    console 放行开关（见 docker-compose.yml 的说明），读进来会让「预期报错」的用例
    悄悄变成「没报错」，且只在写了 .env 的机器上复现——最难查的一类假绿。
    """
    return Settings(_env_file=None, **kw)


def _prod_env(monkeypatch):
    """构造一组「其余生产守卫都能过」的环境，便于单独测通知那一项。"""
    monkeypatch.setenv("DEBUG", "False")
    monkeypatch.setenv("SECRET_KEY", "prod-strong-secret-not-dev-default-1234567890")
    monkeypatch.setenv("COOKIE_SECURE", "True")
    monkeypatch.setenv("OTP_DEV_RETURN_CODE", "False")


def test_prod_forbids_console_sender(monkeypatch):
    """生产用 console = 验证码只进日志：用户收不到，且看日志的人能冒用任意账号。"""
    _prod_env(monkeypatch)
    monkeypatch.setenv("OTP_SENDER", "console")
    with pytest.raises(ValueError, match="console"):
        _settings()


def test_console_allowed_in_prod_only_with_explicit_flag(monkeypatch):
    """端到端验收（脚本从库里读验证码，没有真实渠道）需要放行 console，但必须显式开开关。"""
    _prod_env(monkeypatch)
    monkeypatch.setenv("OTP_SENDER", "console")
    monkeypatch.setenv("OTP_ALLOW_CONSOLE_IN_PROD", "true")
    _settings()  # 不抛即为通过


def test_smtp_requires_host_and_from(monkeypatch):
    """选了 smtp 却没填主机 / 发件人：启动即报错，别等用户注册时才炸。"""
    monkeypatch.setenv("OTP_SENDER", "console")
    monkeypatch.setenv("OTP_EMAIL_SENDER", "smtp")
    monkeypatch.setenv("SMTP_HOST", "")
    with pytest.raises(ValueError, match="SMTP_HOST"):
        _settings()

    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "")
    monkeypatch.setenv("SMTP_USER", "")
    with pytest.raises(ValueError, match="发件人"):
        _settings()


def test_email_cannot_use_webhook(monkeypatch):
    """webhook 是短信网关语义（没有收件人/主题），落到邮件渠道要显式拦住。"""
    monkeypatch.setenv("OTP_SENDER", "webhook")
    monkeypatch.setenv("SMS_WEBHOOK_URL", "https://sms.example.com/send")
    with pytest.raises(ValueError, match="webhook"):
        _settings()


def test_sms_cannot_use_smtp(monkeypatch):
    """短信渠道的 target 是号码，走 smtp 发不出去（真要邮件转短信，应配成 webhook）。"""
    monkeypatch.setenv("OTP_SENDER", "smtp")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "no-reply@example.com")
    with pytest.raises(ValueError, match="smtp"):
        _settings()


def test_webhook_requires_url_and_code_placeholder(monkeypatch):
    monkeypatch.setenv("OTP_SENDER", "webhook")
    monkeypatch.setenv("OTP_EMAIL_SENDER", "console")
    monkeypatch.setenv("SMS_WEBHOOK_URL", "")
    with pytest.raises(ValueError, match="SMS_WEBHOOK_URL"):
        _settings()

    # 模板少了 {code}：接口照样 200、用户照样收不到码，只能靠启动拦住
    monkeypatch.setenv("SMS_WEBHOOK_URL", "https://sms.example.com/send")
    monkeypatch.setenv("SMS_WEBHOOK_BODY", '{"to":"{target}","text":"hello"}')
    with pytest.raises(ValueError, match=r"\{code\}"):
        _settings()


def test_webhook_headers_must_be_json(monkeypatch):
    """头部 JSON 提前解析：否则「启动正常、第一次发短信才炸」。"""
    monkeypatch.setenv("OTP_SENDER", "webhook")
    monkeypatch.setenv("OTP_EMAIL_SENDER", "console")
    monkeypatch.setenv("SMS_WEBHOOK_URL", "https://sms.example.com/send")
    monkeypatch.setenv("SMS_WEBHOOK_BODY", '{"to":"{target}","code":"{code}"}')
    monkeypatch.setenv("SMS_WEBHOOK_HEADERS", "Authorization: Bearer abc")  # 不是 JSON
    with pytest.raises(ValueError, match="SMS_WEBHOOK_HEADERS"):
        _settings()


def test_prod_valid_notification_config(monkeypatch):
    """邮件 SMTP + 短信网关的完整生产配置应当能正常构造。"""
    _prod_env(monkeypatch)
    monkeypatch.setenv("OTP_SENDER", "console")
    monkeypatch.setenv("OTP_EMAIL_SENDER", "smtp")
    monkeypatch.setenv("OTP_SMS_SENDER", "webhook")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "no-reply@example.com")
    monkeypatch.setenv("SMTP_USER", "noreply")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("SMS_WEBHOOK_URL", "https://sms.example.com/send")
    s = _settings()
    assert s.OTP_EMAIL_SENDER == "smtp"
    assert s.OTP_SMS_SENDER == "webhook"
