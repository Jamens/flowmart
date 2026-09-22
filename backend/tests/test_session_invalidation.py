"""改密后旧令牌失效（会话失效）测试。

背景：找回密码若踢不掉已泄露的会话，重置就失去意义——持有旧访问令牌 / 旧刷新令牌的人
仍能继续访问（刷新令牌更是能无限续期）。做法：User.pwd_changed_at 记录最后一次改密时间
（UTC、整秒），令牌签发时间 iat 早于该值即判失效。

注意：这里一律用 raw_client（真实鉴权），不能混用 client 夹具——它会覆盖
get_current_user 直接返回用户对象，从而绕过令牌校验，测不出失效。
"""
import time

from app.core.security import hash_password
from app.models.ecommerce import User

API = "/api/v1"

# iat 与 pwd_changed_at 都是整秒精度，令牌与改密落在同一秒时比较结果相等（不判失效）。
# 睡过 1 秒，保证旧令牌确实早于改密时间，测试才确定。
_IAT_GRACE_SECONDS = 1.05


def _make_verified_user(db, username, email, password="123456"):
    u = User(username=username, email=email, email_verified=True,
             password_hash=hash_password(password), is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _login(raw_client, username, password):
    r = raw_client.post(f"{API}/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_token_still_valid_when_password_never_changed(raw_client, db):
    """pwd_changed_at 为 NULL（从未改密）时令牌保持有效 —— 存量数据向后兼容，无需刷数据。"""
    _make_verified_user(db, "never", "never@example.com", password="abc12345")
    tok = _login(raw_client, "never", "abc12345")["access_token"]
    assert raw_client.get(f"{API}/auth/me", headers=_bearer(tok)).status_code == 200


def test_old_access_token_invalidated_after_reset(raw_client, db):
    _make_verified_user(db, "victim", "victim@example.com", password="oldpass1")
    tok = _login(raw_client, "victim", "oldpass1")["access_token"]

    time.sleep(_IAT_GRACE_SECONDS)
    send = raw_client.post(
        f"{API}/auth/password/reset/send", json={"username": "victim", "channel": "email"}
    ).json()
    conf = raw_client.post(
        f"{API}/auth/password/reset/confirm",
        json={"username": "victim", "channel": "email",
              "code": send["dev_code"], "new_password": "freshpass1"},
    )
    assert conf.status_code == 200

    # 改密前签发的令牌立即失效
    assert raw_client.get(f"{API}/auth/me", headers=_bearer(tok)).status_code == 401
    # 改密后新签发的令牌不受影响（否则等于把刚登录的用户也踢下线）
    new_tok = _login(raw_client, "victim", "freshpass1")["access_token"]
    assert raw_client.get(f"{API}/auth/me", headers=_bearer(new_tok)).status_code == 200


def test_old_refresh_token_invalidated_after_reset(raw_client, db):
    """刷新令牌同样受约束：否则改密后拿旧刷新令牌仍能无限续期，会话失效形同虚设。"""
    _make_verified_user(db, "victim2", "victim2@example.com", password="oldpass1")
    refresh = _login(raw_client, "victim2", "oldpass1")["refresh_token"]

    time.sleep(_IAT_GRACE_SECONDS)
    send = raw_client.post(
        f"{API}/auth/password/reset/send", json={"username": "victim2", "channel": "email"}
    ).json()
    assert raw_client.post(
        f"{API}/auth/password/reset/confirm",
        json={"username": "victim2", "channel": "email",
              "code": send["dev_code"], "new_password": "freshpass1"},
    ).status_code == 200

    assert raw_client.post(f"{API}/auth/refresh", headers=_bearer(refresh)).status_code == 401


def test_stale_token_not_honored_by_optional_auth(raw_client, db):
    """改密后旧令牌在 OTP 入口（get_optional_current_user）同样不被承认。

    若仍被承认，请求会以「已登录」身份放行（200）；失效后应退回未登录，
    既无令牌又没带账号密码 -> 401。
    """
    _make_verified_user(db, "victim3", "victim3@example.com", password="oldpass1")
    tok = _login(raw_client, "victim3", "oldpass1")["access_token"]

    time.sleep(_IAT_GRACE_SECONDS)
    send = raw_client.post(
        f"{API}/auth/password/reset/send", json={"username": "victim3", "channel": "email"}
    ).json()
    assert raw_client.post(
        f"{API}/auth/password/reset/confirm",
        json={"username": "victim3", "channel": "email",
              "code": send["dev_code"], "new_password": "freshpass1"},
    ).status_code == 200

    r = raw_client.post(
        f"{API}/auth/verification/send", headers=_bearer(tok),
        json={"channel": "email", "target": "other@example.com"},
    )
    assert r.status_code == 401


def test_old_access_token_invalidated_after_change_password(raw_client, current_user, db):
    """登录态改密（PATCH /auth/me/password）同样作废旧令牌。

    current_user 夹具只用来建出 tester 账号，鉴权仍走 raw_client 的真实令牌校验。
    """
    tok = _login(raw_client, "tester", "123456")["access_token"]

    time.sleep(_IAT_GRACE_SECONDS)
    r = raw_client.patch(
        f"{API}/auth/me/password", headers=_bearer(tok),
        json={"old_password": "123456", "new_password": "newpass123"},
    )
    assert r.status_code == 200

    assert raw_client.get(f"{API}/auth/me", headers=_bearer(tok)).status_code == 401
    assert _login(raw_client, "tester", "newpass123")["access_token"]
