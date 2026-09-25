"""通用限流：注册 / 发验证码 / 找回密码。

登录限流只记**失败**（防密码爆破），但这些端点不看成败只看调用量——
成功调用同样消耗配额，否则攻击者可用正确参数高频调用批量灌账号、
把短信/邮件渠道打爆（有成本且骚扰他人）。

关键测试设计：发码类端点要造**多个不同用户**来触发同一 IP 的限额。
若复用同一个用户，会先撞上 DB 层的「同用户重发限流」（OTP_MAX_PER_WINDOW），
那就测不到本次加的 IP 维度限流了。
"""
from app.core.config import settings
from app.core.security import hash_password
from app.models.ecommerce import User


def _mk_user(db, username: str, phone: str, verified: bool = False) -> User:
    """verified=False 用于「申请验证码」场景（未验证才需要验证）；
    verified=True 用于找回密码 / 登录（这两处要求联系方式已验证）。"""
    u = User(username=username, nickname=username, phone=phone,
             password_hash=hash_password("secret1"), is_admin=False,
             phone_verified=verified)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _register(raw_client, username: str):
    return raw_client.post(
        "/api/v1/auth/register", json={"username": username, "password": "secret1"}
    )


def test_register_over_limit_returns_429(raw_client):
    """超出每 IP 配额后注册必须 429，而不是继续建号。"""
    for i in range(5):
        assert _register(raw_client, f"rl_reg{i}").status_code == 201

    r = _register(raw_client, "rl_reg_over")
    assert r.status_code == 429
    assert r.headers.get("Retry-After"), "429 必须带 Retry-After，便于客户端退避"


def test_register_limit_does_not_block_normal_amount(raw_client, db):
    """配额内不受影响（回归：限流不能误伤正常注册）。"""
    r = _register(raw_client, "rl_normal")
    assert r.status_code == 201, r.text


def test_verification_send_over_limit_returns_429(raw_client, db):
    """发验证码按 IP 限流：换不同用户也躲不过同一出口 IP 的配额。"""
    for i in range(6):
        _mk_user(db, f"rl_v{i}", f"1380000100{i}")

    def send(i):
        return raw_client.post(
            "/api/v1/auth/verification/send",
            json={"channel": "phone", "target": f"1380000100{i}",
                  "username": f"rl_v{i}", "password": "secret1"},
        )

    for i in range(5):
        assert send(i).status_code == 200, send(i).text
    assert send(5).status_code == 429


def test_password_reset_send_over_limit_returns_429(raw_client, db):
    """找回密码同样会触发发码，必须一起限流。"""
    for i in range(6):
        _mk_user(db, f"rl_p{i}", f"1380000200{i}", verified=True)

    def send(i):
        return raw_client.post(
            "/api/v1/auth/password/reset/send",
            json={"username": f"rl_p{i}", "channel": "phone"},
        )

    for i in range(5):
        assert send(i).status_code == 200, send(i).text
    assert send(5).status_code == 429


def test_login_over_ip_limit_returns_429(raw_client, monkeypatch):
    """登录的 per-IP 上限：换用户名也躲不过。

    登录失败限流是 per (IP, 用户名)，换用户名就能重置配额；而每次密码校验都要跑
    PBKDF2（几十毫秒 CPU），于是「用户名 × N 次」就是 CPU 放大 DoS 与凭证填充的
    通道。这一层不看用户名，只认 IP。
    """
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_MAX", 2)
    for i in range(2):
        r = raw_client.post(
            "/api/v1/auth/login", json={"username": f"rl_nx{i}", "password": "secret1"}
        )
        assert r.status_code != 429, r.text
    # 第 3 次换了个不存在的用户名，仍应被 per-IP 配额拦住
    r = raw_client.post(
        "/api/v1/auth/login", json={"username": "rl_nx9", "password": "secret1"}
    )
    assert r.status_code == 429


def test_otp_confirm_over_limit_returns_429(raw_client, db, monkeypatch):
    """OTP 确认限流：6 位纯数字码 + 10 分钟 TTL，不限住就是爆破通道。

    不能只靠每条码的 OTP_CONFIRM_MAX_ATTEMPTS——重新发码会作废旧码、
    新码又给满 5 次，稳态猜码速率 = 尝试数/码 × 码数/分。
    """
    monkeypatch.setattr(settings, "RATE_LIMIT_OTP_MAX", 2)
    _mk_user(db, "rl_otp", "13800005000")

    for _ in range(3):
        r = raw_client.post(
            "/api/v1/auth/verification/confirm",
            json={"channel": "phone", "target": "13800005000", "code": "000000",
                  "username": "rl_otp", "password": "secret1"},
        )
    assert r.status_code == 429


def test_login_limit_and_register_limit_are_independent(raw_client, db):
    """两套限流各自独立：注册被限不应影响登录路径。"""
    for i in range(6):
        _register(raw_client, f"rl_both{i}")
    assert _register(raw_client, "rl_both_over").status_code == 429

    # 注册已达上限，但登录走的是另一个限流器（只记失败），不应被连坐
    _mk_user(db, "rl_login_ok", "13800003000", verified=True)
    r = raw_client.post("/api/v1/auth/login",
                        json={"username": "rl_login_ok", "password": "secret1"})
    assert r.status_code == 200, r.text
