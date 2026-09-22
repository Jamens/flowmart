"""认证安全：httpOnly Cookie 下发 + 回读，以及 CORS 收紧。

验证 code review P3 的收口：
- 登录/注册写入 httpOnly Cookie（XSS 读不到），且令牌也可经 Cookie 回读认证；
- CORS 仅放行配置中的已知前端源，不再 `*`。
"""
def _verify(raw_client, token, target="verified@example.com", channel="email"):
    h = {"Authorization": f"Bearer {token}"}
    code = raw_client.post(
        "/api/v1/auth/verification/send", headers=h, json={"channel": channel, "target": target}
    ).json()["dev_code"]
    r = raw_client.post(
        "/api/v1/auth/verification/confirm", headers=h,
        json={"channel": channel, "target": target, "code": code},
    )
    assert r.status_code == 200, r.text


def test_login_sets_httponly_cookie(raw_client):
    reg = raw_client.post("/api/v1/auth/register", json={"username": "cookieuser", "password": "secret1"}).json()
    _verify(raw_client, reg["access_token"])
    r = raw_client.post("/api/v1/auth/login", json={"username": "cookieuser", "password": "secret1"})
    assert r.status_code == 200
    sc = r.headers.get("set-cookie", "")
    assert "fm_token=" in sc
    assert "HttpOnly" in sc  # JS 无法读取，防御 XSS 窃令牌
    assert "Secure" not in sc  # 开发期 COOKIE_SECURE=False，仅 HTTPS 才加 Secure


def test_request_via_cookie_authenticates(raw_client):
    reg = raw_client.post("/api/v1/auth/register", json={"username": "cookieuser2", "password": "secret1"}).json()
    _verify(raw_client, reg["access_token"], "c2@example.com")
    login = raw_client.post(
        "/api/v1/auth/login", json={"username": "cookieuser2", "password": "secret1"}
    ).json()
    token = login["access_token"]
    # 不带 Bearer，仅带 Cookie，应仍能鉴权
    r = raw_client.get("/api/v1/auth/me", cookies={"fm_token": token})
    assert r.status_code == 200
    assert r.json()["username"] == "cookieuser2"


def test_cors_echoes_configured_origin(raw_client):
    # 必须用真实存在的 /health 端点：若误用 404 路径，CORS 中间件对 404 也回显头会掩盖盲区
    r = raw_client.get("/health", headers={"Origin": "http://127.0.0.1:5173"})
    assert r.status_code == 200  # 端点确实存在，CORS 头才有意义
    assert r.headers.get("access-control-allow-origin") == "http://127.0.0.1:5173"
    assert r.headers.get("access-control-allow-credentials") == "true"


def test_cors_rejects_unknown_origin(raw_client):
    # 用真实存在的 /health 端点；未知源不应被回显，杜绝任意站点带凭据跨域调用
    r = raw_client.get("/health", headers={"Origin": "http://evil.example.com"})
    assert r.status_code == 200
    # Starlette 对非白名单源根本不回显该头（而非回显错误值），用 is None 表达真实安全意图；
    # 注意：allow-credentials 头会随 allow_credentials=True 配置始终输出 true，但它单独出现无危害——
    # 浏览器仅在 allow-origin 精确匹配时才允许携带凭据，未知源无匹配的 allow-origin 即被拦截。
    assert r.headers.get("access-control-allow-origin") is None


def test_logout_clears_cookie(raw_client):
    reg = raw_client.post("/api/v1/auth/register", json={"username": "logoutuser", "password": "secret1"}).json()
    _verify(raw_client, reg["access_token"], "lo@example.com")
    raw_client.post("/api/v1/auth/login", json={"username": "logoutuser", "password": "secret1"})
    r = raw_client.post("/api/v1/auth/logout")
    assert r.status_code == 200
    sc = r.headers.get("set-cookie", "")
    # 退出登录必须把 Cookie 置空并 Max-Age=0，浏览器随即丢弃
    assert "fm_token=" in sc
    assert "max-age=0" in sc.lower()
    # 清掉后再带一个伪造 Cookie 访问 /me 应 401，证明 Cookie 已失效
    after = raw_client.get("/api/v1/auth/me", cookies={"fm_token": "bogus"})
    assert after.status_code == 401


def test_login_sets_secure_cookie_in_prod(raw_client, monkeypatch):
    # 生产环境 COOKIE_SECURE=True：浏览器只有 HTTPS 才接受该 Cookie，明文 HTTP 下不发送
    monkeypatch.setattr("app.core.security.settings.COOKIE_SECURE", True)
    reg = raw_client.post("/api/v1/auth/register", json={"username": "secureuser", "password": "secret1"}).json()
    _verify(raw_client, reg["access_token"], "su@example.com")
    r = raw_client.post("/api/v1/auth/login", json={"username": "secureuser", "password": "secret1"})
    sc = r.headers.get("set-cookie", "")
    assert "Secure" in sc  # 生产必须带 Secure
    assert "samesite=lax" in sc.lower()  # SameSite 属性正确写入（Starlette 输出小写）


def test_bearer_header_takes_precedence_over_cookie(raw_client):
    # 同时带有效 Bearer 头与伪造 Cookie，应以 Bearer 头身份为准，杜绝 Cookie 混淆/冒充
    reg = raw_client.post("/api/v1/auth/register", json={"username": "precedence", "password": "secret1"}).json()
    _verify(raw_client, reg["access_token"], "pc@example.com")
    token = (
        raw_client.post("/api/v1/auth/login", json={"username": "precedence", "password": "secret1"})
        .json()["access_token"]
    )
    r = raw_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
        cookies={"fm_token": "forged-token"},
    )
    assert r.status_code == 200
    assert r.json()["username"] == "precedence"
