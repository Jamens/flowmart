"""邮箱 / 手机验证码（OTP）接口测试。

覆盖：申请（dev 回传码）、确认成功置 verified、错误码拒绝、过期码拒绝、
同渠道重发限流、手机号验证、/me 含验证状态、注册带 email、格式校验、重复确认被拒。
"""
from datetime import datetime, timedelta

from sqlalchemy import select

from app.core.config import settings
from app.models.ecommerce import VerificationCode

AUTH = "/api/v1/auth"


def _send(client, channel, target):
    return client.post(f"{AUTH}/verification/send", json={"channel": channel, "target": target})


def _confirm(client, channel, target, code):
    return client.post(
        f"{AUTH}/verification/confirm",
        json={"channel": channel, "target": target, "code": code},
    )


def test_send_returns_dev_code_and_stores_row(client, db):
    """开发环境申请验证码：返回 dev_code，且数据库落了一条未消费的记录。"""
    r = _send(client, "email", "alice@example.com")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sent"] is True
    assert body["dev_code"]
    row = db.execute(
        select(VerificationCode).where(
            VerificationCode.target == "alice@example.com",
            VerificationCode.consumed_at.is_(None),
        )
    ).scalars().first()
    assert row is not None
    assert row.code == body["dev_code"]


def test_confirm_success_sets_email_verified(client, db):
    """确认正确验证码：email_verified 置 True，且邮箱绑定到账号。"""
    code = _send(client, "email", "bob@example.com").json()["dev_code"]
    r = _confirm(client, "email", "bob@example.com", code)
    assert r.status_code == 200, r.text
    assert r.json()["email_verified"] is True

    me = client.get(f"{AUTH}/me").json()
    assert me["email"] == "bob@example.com"
    assert me["email_verified"] is True


def test_confirm_wrong_code_rejected(client):
    code = _send(client, "email", "wrong@example.com").json()["dev_code"]
    r = _confirm(client, "email", "wrong@example.com", "000000")
    assert r.status_code == 400
    assert "验证码" in r.json()["detail"]


def test_confirm_expired_code_rejected(client, db):
    """直接造一条已过期的码，确认应被拒（无效或已过期）。"""
    past = datetime.now() - timedelta(seconds=1)
    db.add(
        VerificationCode(
            user_id=client.user.id, channel="email", target="exp@example.com",
            code="111111", purpose="verify", expires_at=past,
        )
    )
    db.commit()
    r = _confirm(client, "email", "exp@example.com", "111111")
    assert r.status_code == 400


def test_confirm_consumed_code_rejected(client):
    """消费过的码不能二次使用。"""
    code = _send(client, "email", "reuse@example.com").json()["dev_code"]
    assert _confirm(client, "email", "reuse@example.com", code).status_code == 200
    assert _confirm(client, "email", "reuse@example.com", code).status_code == 400


def test_resend_rate_limit(client):
    """同一渠道在窗口内超过 OTP_MAX_PER_WINDOW 次即 429。"""
    target = "ratelimit@example.com"
    ok = 0
    for _ in range(6):
        r = _send(client, "email", target)
        if r.status_code == 200:
            ok += 1
        elif r.status_code == 429:
            break
    assert ok == 5  # 默认 OTP_MAX_PER_WINDOW=5，第 6 次被限流


def test_phone_verification(client):
    code = _send(client, "phone", "13800001111").json()["dev_code"]
    r = _confirm(client, "phone", "13800001111", code)
    assert r.status_code == 200
    assert r.json()["phone_verified"] is True


def test_invalid_email_format_rejected(client):
    r = _send(client, "email", "not-an-email")
    assert r.status_code == 400


def test_invalid_channel_rejected(client):
    r = client.post(
        f"{AUTH}/verification/send", json={"channel": "fax", "target": "x@y.com"}
    )
    assert r.status_code == 422  # pydantic Literal 校验


def test_confirm_exceeds_attempts_locks_code(client):
    """错误码超过上限后该码被锁定，之后即便输入正确码也被拒（防对 6 位码暴力枚举）。"""
    code = _send(client, "email", "lock@example.com").json()["dev_code"]
    for _ in range(settings.OTP_CONFIRM_MAX_ATTEMPTS):
        r = _confirm(client, "email", "lock@example.com", "000000")
        assert r.status_code == 400
    # 锁定后用正确码也应被拒
    assert _confirm(client, "email", "lock@example.com", code).status_code == 400
    # 重新申请一个码仍可正常验证（锁定只影响被爆破的那条）
    code2 = _send(client, "email", "lock@example.com").json()["dev_code"]
    assert _confirm(client, "email", "lock@example.com", code2).status_code == 200


def test_register_with_email(raw_client):
    """注册带 email：落库后登录用真实 Bearer 令牌取 /me，应回显邮箱且未验证。

    注意必须用 raw_client（真实鉴权链路），不能用品客的 client——后者把 get_current_user
    覆写成固定夹具用户，Bearer 令牌会被忽略，/me 永远返回夹具用户而非刚注册的用户。
    """
    r = raw_client.post(
        f"{AUTH}/register",
        json={"username": "emailreg", "password": "123456", "email": "reg@example.com"},
    )
    assert r.status_code == 201, r.text
    me = raw_client.post(f"{AUTH}/login", json={"username": "emailreg", "password": "123456"})
    token = me.json()["access_token"]
    profile = raw_client.get(f"{AUTH}/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert profile["email"] == "reg@example.com"
    assert profile["email_verified"] is False
