"""JWT 刷新/续期：/auth/refresh 换发访问令牌、access/refresh 类型隔离、过期访问续期、登出清双 Cookie。

验证点：
- 浏览器凭刷新 Cookie 静默换发新访问令牌并可用于 /me；
- 无刷新令牌调 /auth/refresh 必须 401；
- 刷新令牌不能当访问令牌用（type 隔离，防令牌混用冒充）；
- 过期的访问令牌经 refresh 后续期可用；
- 登出同时清除访问与刷新两个 Cookie。
"""
from app.core.security import create_access_token, create_refresh_token


def _verify(raw_client, token, target="verified@example.com", channel="email"):
    """测试夹具：用令牌走 OTP 验证（注册后立刻调用，便于后续登录通过登录验证闸门）。"""
    h = {"Authorization": f"Bearer {token}"}
    code = raw_client.post(
        "/api/v1/auth/verification/send", headers=h,
        json={"channel": channel, "target": target}
    ).json()["dev_code"]
    r = raw_client.post(
        "/api/v1/auth/verification/confirm", headers=h,
        json={"channel": channel, "target": target, "code": code},
    )
    assert r.status_code == 200, r.text


def test_refresh_issues_new_access_and_authenticates(raw_client):
    reg = raw_client.post("/api/v1/auth/register", json={"username": "ru1", "password": "secret1"}).json()
    _verify(raw_client, reg["access_token"])
    raw_client.post("/api/v1/auth/login", json={"username": "ru1", "password": "secret1"})
    # 凭刷新 Cookie 换发新访问令牌
    r = raw_client.post("/api/v1/auth/refresh")
    assert r.status_code == 200, r.text
    new_access = r.json()["access_token"]
    # 新访问令牌可用
    me = raw_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {new_access}"})
    assert me.status_code == 200
    assert me.json()["username"] == "ru1"


def test_refresh_requires_valid_refresh_token(raw_client):
    # 没带刷新 Cookie → 401
    r = raw_client.post("/api/v1/auth/refresh")
    assert r.status_code == 401
    assert "刷新令牌" in r.json()["detail"]


def test_refresh_token_cannot_be_used_as_access(raw_client):
    # 刷新令牌不能当访问令牌用（type 隔离）
    refresh = create_refresh_token(1)
    r = raw_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {refresh}"})
    assert r.status_code == 401


def test_expired_access_token_refreshed_then_works(raw_client):
    reg = raw_client.post("/api/v1/auth/register", json={"username": "ru2", "password": "secret1"}).json()
    _verify(raw_client, reg["access_token"])
    raw_client.post("/api/v1/auth/login", json={"username": "ru2", "password": "secret1"})
    # 塞一个已过期的访问令牌进 Cookie
    expired = create_access_token(1, expires_minutes=-1)
    raw_client.cookies.set("fm_token", expired)
    # 过期访问令牌 → 401
    assert raw_client.get("/api/v1/auth/me").status_code == 401
    # 刷新换发新访问令牌 + 回写新的访问 Cookie（浏览器靠它静默续期）
    r = raw_client.post("/api/v1/auth/refresh")
    assert r.status_code == 200, r.text
    sc = r.headers.get("set-cookie", "")
    assert "fm_token=" in sc  # 刷新端点必须回写访问 Cookie
    new_access = r.json()["access_token"]
    # 新访问令牌可用
    me = raw_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {new_access}"})
    assert me.status_code == 200
    assert me.json()["username"] == "ru2"


def test_logout_clears_both_cookies(raw_client):
    raw_client.post("/api/v1/auth/register", json={"username": "ru3", "password": "secret1"})
    raw_client.post("/api/v1/auth/login", json={"username": "ru3", "password": "secret1"})
    r = raw_client.post("/api/v1/auth/logout")
    sc = r.headers.get("set-cookie", "")
    # 访问与刷新两个 Cookie 都必须被置空（max-age=0）
    assert "fm_token=" in sc and "max-age=0" in sc.lower()
    assert "fm_refresh=" in sc and "max-age=0" in sc.lower()


def test_expired_refresh_token_rejected(raw_client, monkeypatch):
    # 刷新令牌过期 → /auth/refresh 必须 401（逻辑在 decode_refresh_token 的过期校验）
    monkeypatch.setattr("app.core.security.settings.REFRESH_TOKEN_EXPIRE_DAYS", -1)
    expired_refresh = create_refresh_token(1)
    r = raw_client.post(
        "/api/v1/auth/refresh", headers={"Authorization": f"Bearer {expired_refresh}"}
    )
    assert r.status_code == 401


def test_access_token_cannot_be_used_as_refresh(raw_client):
    # 反向隔离：访问令牌不能当刷新令牌用（type 必须为 refresh）
    access = create_access_token(1)
    r = raw_client.post(
        "/api/v1/auth/refresh", headers={"Authorization": f"Bearer {access}"}
    )
    assert r.status_code == 401
