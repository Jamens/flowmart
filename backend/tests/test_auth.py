"""鉴权测试：覆盖真实鉴权链路与底层 JWT / 密码哈希。

为什么单独用 raw_client：其它用例通过覆盖 get_current_user 直接「假装已登录」，
只有这里走真实的 HTTPBearer 依赖，专门验证「没带令牌就 401、带令牌才放行」。
"""
import pytest

from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


# ---------------- 底层安全函数 ----------------


def test_password_hash_roundtrip():
    h = hash_password("s3cret")
    assert h != "s3cret"
    assert verify_password("s3cret", h) is True
    assert verify_password("wrong", h) is False
    # 同密码两次哈希结果不同（盐随机），但都能验证通过
    assert hash_password("s3cret") != h


def test_jwt_roundtrip_and_tamper():
    tok = create_access_token(42)
    payload = decode_access_token(tok)
    assert payload["sub"] == "42"

    # 篡改签名必须被拒
    with pytest.raises(Exception):
        decode_access_token(tok + "x")

    # 随便造一个假令牌（签名不对）必须被拒
    fake = "a.b.c"
    with pytest.raises(Exception):
        decode_access_token(fake)


# ---------------- 真实鉴权链路 ----------------


def test_register_returns_token(raw_client):
    r = raw_client.post(
        "/api/v1/auth/register",
        json={"username": "alice", "password": "secret1", "nickname": "爱丽丝"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["token_type"] == "bearer"
    assert r.json()["access_token"]


def test_login_wrong_password_rejected(raw_client):
    raw_client.post("/api/v1/auth/register", json={"username": "bob", "password": "secret1"})
    r = raw_client.post("/api/v1/auth/login", json={"username": "bob", "password": "nope"})
    assert r.status_code == 401


def test_login_success_returns_token(raw_client):
    raw_client.post("/api/v1/auth/register", json={"username": "carol", "password": "secret1"})
    r = raw_client.post("/api/v1/auth/login", json={"username": "carol", "password": "secret1"})
    assert r.status_code == 200, r.text
    assert r.json()["access_token"]


def test_me_requires_auth(raw_client):
    assert raw_client.get("/api/v1/auth/me").status_code == 401


def test_protected_endpoint_requires_auth(raw_client):
    # 购物车是受保护资源，未登录必须 401，不能靠请求体里的 user_id 混进去
    assert raw_client.get("/api/v1/cart").status_code == 401


def test_protected_endpoint_with_token_works(raw_client, db):
    from app.models.ecommerce import User

    u = User(username="dave", password_hash=hash_password("secret1"))
    db.add(u)
    db.commit()

    tok = create_access_token(u.id)
    headers = {"Authorization": f"Bearer {tok}"}
    r = raw_client.get("/api/v1/cart", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == u.id


def test_duplicate_username_rejected(raw_client):
    raw_client.post("/api/v1/auth/register", json={"username": "eve", "password": "secret1"})
    r = raw_client.post("/api/v1/auth/register", json={"username": "eve", "password": "secret1"})
    assert r.status_code == 400


def test_malformed_token_rejected(raw_client):
    # 坏签名 / 坏 base64 必须 401，不能 500
    assert raw_client.get("/api/v1/cart", headers={"Authorization": "Bearer not.a.jwt"}).status_code == 401
    assert raw_client.get("/api/v1/cart", headers={"Authorization": "Bearer xxx"}).status_code == 401


def test_cross_user_cannot_read_address(raw_client):
    """真实双令牌验证：用户 B 拿不到用户 A 的地址（_assert_owner 拦截）。"""
    a = raw_client.post("/api/v1/auth/register", json={"username": "userA", "password": "secret1"}).json()
    b = raw_client.post("/api/v1/auth/register", json={"username": "userB", "password": "secret1"}).json()
    token_a = raw_client.post("/api/v1/auth/login", json={"username": "userA", "password": "secret1"}).json()["access_token"]
    token_b = raw_client.post("/api/v1/auth/login", json={"username": "userB", "password": "secret1"}).json()["access_token"]
    h_a = {"Authorization": f"Bearer {token_a}"}
    h_b = {"Authorization": f"Bearer {token_b}"}

    # A 给自己建地址
    addr = raw_client.post(
        f"/api/v1/users/{a['id']}/addresses",
        headers=h_a,
        json={"receiver": "A先生", "phone": "13800000001"},
    ).json()
    assert addr["id"]

    # B 读/改 A 的地址必须 404
    assert raw_client.get(f"/api/v1/users/{a['id']}/addresses", headers=h_b).status_code == 404
    assert raw_client.patch(
        f"/api/v1/users/{a['id']}/addresses/{addr['id']}", headers=h_b, json={"receiver": "黑客"}
    ).status_code == 404
    # A 自己读得到
    assert raw_client.get(f"/api/v1/users/{a['id']}/addresses", headers=h_a).status_code == 200


def test_secret_key_guard_blocks_default_in_prod():
    """生产（非 debug）下仍用默认弱密钥必须启动即报错，否则任何人都能伪造令牌。

    这是 code review 后的加固项：guard 曾在一次静默丢改中丢失导致生产可被令牌伪造，
    用本测试把「修复」锁死，避免再次回归。
    """
    from pydantic import ValidationError

    from app.core.config import Settings

    # 非 debug + 默认弱密钥 => 必须抛 ValidationError
    with pytest.raises(ValidationError):
        Settings(DEBUG=False, SECRET_KEY="dev-only-insecure-secret-change-me")

    # 显式指定强密钥 + COOKIE_SECURE=True（生产必须）则可正常构造
    s = Settings(
        DEBUG=False,
        SECRET_KEY="a-strong-random-prod-secret-at-least-32-chars",
        COOKIE_SECURE=True,
        OTP_DEV_RETURN_CODE=False,  # 生产必须关掉验证码明文回传
    )
    assert s.SECRET_KEY == "a-strong-random-prod-secret-at-least-32-chars"
