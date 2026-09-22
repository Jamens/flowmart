"""登录验证闸门测试。

登录强约束：未验证邮箱/手机的用户无法登录（403）；但可通过
`/auth/verification/send` + `/confirm` 凭账号密码自证身份（未登录自助验证），
验证后再登录即成功，不会被永久锁死。

覆盖：
- 未验证用户登录 → 403；
- 未登录凭账号密码自助 OTP 验证 → 再登录 200；
- 已验证用户登录 → 200；
- 撤销验证（email/phone 都置 False）后再登录 → 403。
"""
from sqlalchemy import select

from app.models.ecommerce import User


def _register(raw_client, username, password="secret1"):
    r = raw_client.post("/api/v1/auth/register", json={"username": username, "password": password})
    assert r.status_code == 201, r.text
    return r.json()


def _self_service_verify(raw_client, username, password, target="gate@example.com", channel="email"):
    """未登录自助验证：凭账号密码自证身份，无需先登录。"""
    code = raw_client.post(
        "/api/v1/auth/verification/send",
        json={"channel": channel, "target": target, "username": username, "password": password},
    ).json()["dev_code"]
    r = raw_client.post(
        "/api/v1/auth/verification/confirm",
        json={"channel": channel, "target": target, "code": code, "username": username, "password": password},
    )
    assert r.status_code == 200, r.text


def test_unverified_login_blocked(raw_client):
    """未验证（新注册）用户登录必须被 403 拦截。"""
    _register(raw_client, "gate_unv")
    r = raw_client.post("/api/v1/auth/login", json={"username": "gate_unv", "password": "secret1"})
    assert r.status_code == 403, r.text
    assert "验证" in r.json()["detail"]


def test_self_service_verify_then_login(raw_client):
    """未登录凭账号密码走 OTP 自助验证，验证后再登录成功（不会被永久锁死）。

    关键：register 会写 httpOnly Cookie，这里清掉它，确保走的是「无令牌 + 账号密码」的
    自助路径（否则 get_optional_current_user 会凭 Cookie 拿到用户，掩盖 user=None 时的 500 隐患）。
    """
    _register(raw_client, "gate_self")
    raw_client.cookies.clear()  # 模拟「已登出、无令牌」场景
    _self_service_verify(raw_client, "gate_self", "secret1", target="gate_self@example.com")
    r = raw_client.post("/api/v1/auth/login", json={"username": "gate_self", "password": "secret1"})
    assert r.status_code == 200, r.text
    assert r.json()["access_token"]


def test_verified_user_can_login(raw_client):
    """已验证用户登录正常放行。"""
    reg = _register(raw_client, "gate_ok")
    token = reg["access_token"]
    code = raw_client.post(
        "/api/v1/auth/verification/send",
        headers={"Authorization": f"Bearer {token}"},
        json={"channel": "email", "target": "gate_ok@example.com"},
    ).json()["dev_code"]
    c = raw_client.post(
        "/api/v1/auth/verification/confirm",
        headers={"Authorization": f"Bearer {token}"},
        json={"channel": "email", "target": "gate_ok@example.com", "code": code},
    )
    assert c.status_code == 200, c.text
    r = raw_client.post("/api/v1/auth/login", json={"username": "gate_ok", "password": "secret1"})
    assert r.status_code == 200, r.text


def test_revoked_verification_blocks_login(raw_client, db):
    """验证状态取运行时值：撤销验证后再登录重新被 403 拦截。"""
    reg = _register(raw_client, "gate_rev")
    token = reg["access_token"]
    code = raw_client.post(
        "/api/v1/auth/verification/send",
        headers={"Authorization": f"Bearer {token}"},
        json={"channel": "email", "target": "gate_rev@example.com"},
    ).json()["dev_code"]
    c = raw_client.post(
        "/api/v1/auth/verification/confirm",
        headers={"Authorization": f"Bearer {token}"},
        json={"channel": "email", "target": "gate_rev@example.com", "code": code},
    )
    assert c.status_code == 200, c.text
    # 验证后登录成功
    assert raw_client.post("/api/v1/auth/login", json={"username": "gate_rev", "password": "secret1"}).status_code == 200
    # 撤销验证
    u = db.execute(select(User).where(User.username == "gate_rev")).scalars().first()
    u.email_verified = False
    u.phone_verified = False
    db.commit()
    # 再次登录被拦
    r = raw_client.post("/api/v1/auth/login", json={"username": "gate_rev", "password": "secret1"})
    assert r.status_code == 403, r.text
