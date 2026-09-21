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
