"""刷新令牌轮转（rotation）与重放检测测试。

无服务端状态时「换发新码但旧码仍有效」只是安全假象，挡不住泄露令牌被无限重放。
现在每条刷新令牌在 refresh_tokens 表留痕：用过后置 used_at，再次出现即判重放，
并撤销同一 family（同一次登录会话派生出的整条轮转链）。

关键细节：/auth/refresh 优先读 Cookie，轮转后又会把新令牌写回 Cookie。
若不先清 Cookie，后续请求会一直用新令牌，测不出「旧令牌被拒」。
因此这里登录后一律 clear cookies、用 Bearer 显式指定要测的令牌。
"""
from app.core.security import create_refresh_token, hash_password
from app.models.ecommerce import User

API = "/api/v1"


def _make_verified_user(db, username, email, password="secret1"):
    u = User(username=username, email=email, email_verified=True,
             password_hash=hash_password(password), is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _login(raw_client, username, password):
    r = raw_client.post(f"{API}/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    body = r.json()
    # 清掉 Cookie：后续一律用 Bearer 显式指定被测令牌，避免 Cookie 抢先生效
    raw_client.cookies.clear()
    return body


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _refresh(raw_client, token):
    # 每次成功刷新都会把新令牌写回 Cookie，而 /auth/refresh 优先读 Cookie。
    # 若不清 Cookie，重放测试实际用的是刚签发的新令牌（会误判为 200）。
    raw_client.cookies.clear()
    return raw_client.post(f"{API}/auth/refresh", headers=_bearer(token))


def test_refresh_rotates_and_old_token_rejected(raw_client, db):
    _make_verified_user(db, "rot1", "rot1@example.com")
    old = _login(raw_client, "rot1", "secret1")["refresh_token"]

    r = _refresh(raw_client, old)
    assert r.status_code == 200, r.text
    new = r.json()["refresh_token"]
    assert new and new != old, "轮转必须签发一条不同的新刷新令牌"

    # 轮转后的新令牌可继续用于下一次刷新
    assert _refresh(raw_client, new).status_code == 200
    # 旧令牌已被轮换掉，再次使用必须拒绝
    assert _refresh(raw_client, old).status_code == 401


def test_replay_revokes_whole_family(raw_client, db):
    """已用过的令牌再次出现 = 重放（多半已泄露）：整条 family 一并撤销，强制重新登录。"""
    _make_verified_user(db, "rot2", "rot2@example.com")
    old = _login(raw_client, "rot2", "secret1")["refresh_token"]

    r = _refresh(raw_client, old)
    assert r.status_code == 200
    new = r.json()["refresh_token"]

    assert _refresh(raw_client, old).status_code == 401
    # 同一 family 里刚签发的令牌也失效——不能再拿它继续续期
    assert _refresh(raw_client, new).status_code == 401


def test_logout_revokes_refresh_token(raw_client, db):
    """登出要在服务端撤销：只清 Cookie 的话，泄露的令牌仍可换发访问令牌。"""
    _make_verified_user(db, "rot3", "rot3@example.com")
    refresh = _login(raw_client, "rot3", "secret1")["refresh_token"]

    r = raw_client.post(f"{API}/auth/logout", headers=_bearer(refresh))
    assert r.status_code == 200
    assert _refresh(raw_client, refresh).status_code == 401


def test_untracked_refresh_token_rejected(raw_client, db):
    """没在库里登记的刷新令牌（无 jti），即使签名有效、未过期、用户存在也必须拒绝。

    现有的类型隔离测试用的是不存在的 user id，401 来自「用户不存在」分支；
    这条才真正覆盖到「查不到记录」的分支。
    """
    u = _make_verified_user(db, "rot5", "rot5@example.com")
    untracked = create_refresh_token(u.id)  # 未登记，无 jti
    assert _refresh(raw_client, untracked).status_code == 401


def test_logout_with_access_token_also_revokes(raw_client, db):
    """API 客户端常带访问令牌调登出：此时无法定位 family，应撤销该用户全部刷新令牌。"""
    _make_verified_user(db, "rot6", "rot6@example.com")
    body = _login(raw_client, "rot6", "secret1")
    refresh, access = body["refresh_token"], body["access_token"]

    r = raw_client.post(f"{API}/auth/logout", headers=_bearer(access))
    assert r.status_code == 200
    assert _refresh(raw_client, refresh).status_code == 401


def test_devices_have_independent_families(raw_client, db):
    """多设备并存：两次登录是两条独立 family，一台设备轮转不影响另一台。"""
    _make_verified_user(db, "rot4", "rot4@example.com")
    a = _login(raw_client, "rot4", "secret1")["refresh_token"]
    b = _login(raw_client, "rot4", "secret1")["refresh_token"]
    assert a != b

    ra = _refresh(raw_client, a)
    assert ra.status_code == 200
    # 设备 B 的令牌不受设备 A 轮转影响
    assert _refresh(raw_client, b).status_code == 200
