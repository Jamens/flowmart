"""认证安全：httpOnly Cookie 下发 + 回读，以及 CORS 收紧。

验证 code review P3 的收口：
- 登录/注册写入 httpOnly Cookie（XSS 读不到），且令牌也可经 Cookie 回读认证；
- CORS 仅放行配置中的已知前端源，不再 `*`。
"""
def test_login_sets_httponly_cookie(raw_client):
    raw_client.post("/api/v1/auth/register", json={"username": "cookieuser", "password": "secret1"})
    r = raw_client.post("/api/v1/auth/login", json={"username": "cookieuser", "password": "secret1"})
    assert r.status_code == 200
    sc = r.headers.get("set-cookie", "")
    assert "fm_token=" in sc
    assert "HttpOnly" in sc  # JS 无法读取，防御 XSS 窃令牌
    assert "Secure" not in sc  # 开发期 COOKIE_SECURE=False，仅 HTTPS 才加 Secure


def test_request_via_cookie_authenticates(raw_client):
    raw_client.post("/api/v1/auth/register", json={"username": "cookieuser2", "password": "secret1"})
    login = raw_client.post(
        "/api/v1/auth/login", json={"username": "cookieuser2", "password": "secret1"}
    ).json()
    token = login["access_token"]
    # 不带 Bearer，仅带 Cookie，应仍能鉴权
    r = raw_client.get("/api/v1/auth/me", cookies={"fm_token": token})
    assert r.status_code == 200
    assert r.json()["username"] == "cookieuser2"


def test_cors_echoes_configured_origin(raw_client):
    r = raw_client.get("/api/v1/health", headers={"Origin": "http://127.0.0.1:5173"})
    assert r.headers.get("access-control-allow-origin") == "http://127.0.0.1:5173"
    assert r.headers.get("access-control-allow-credentials") == "true"


def test_cors_rejects_unknown_origin(raw_client):
    r = raw_client.get("/api/v1/health", headers={"Origin": "http://evil.example.com"})
    # 未配置的源不应被回显，杜绝任意站点带凭据跨域调用
    assert r.headers.get("access-control-allow-origin") != "http://evil.example.com"
