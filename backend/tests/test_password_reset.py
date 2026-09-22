"""找回密码（reset）与登录态自助改密（change）测试。

找回密码面向「忘了密码 / 生产空密码账号」这类登不进系统的用户，因此 send/confirm
必须未登录可访问：身份 = 用户名 + 控制已验证联系方式（由 OTP 证明），无需旧密码。
改密则要求已登录（get_current_user），且需提供正确的原密码。
"""
import pytest

from app.core.ratelimit import login_limiter
from app.core.security import hash_password
from app.core.verification import confirm_code, request_code
from app.models.ecommerce import User

API = "/api/v1"


@pytest.fixture(autouse=True)
def _clean_limiter():
    """改密复用了登录限流（进程级单例），每个用例前清空，避免跨用例计数串扰。"""
    login_limiter.reset_all()


def _make_verified_user(db, username, email, password="123456"):
    u = User(
        username=username,
        email=email,
        email_verified=True,
        password_hash=hash_password(password),
        is_active=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_reset_send_requires_verified_channel(raw_client, db):
    # 用户存在但邮箱未验证 -> 400，不能拿未验证渠道找回
    u = User(username="unverified", email="a@b.com", email_verified=False,
             password_hash=hash_password("123456"))
    db.add(u)
    db.commit()
    r = raw_client.post(
        f"{API}/auth/password/reset/send", json={"username": "unverified", "channel": "email"}
    )
    assert r.status_code == 400


def test_reset_send_unknown_user_401(raw_client, db):
    r = raw_client.post(
        f"{API}/auth/password/reset/send", json={"username": "ghost", "channel": "email"}
    )
    assert r.status_code == 401


def test_reset_flow_sets_new_password(raw_client, db):
    _make_verified_user(db, "recover", "recover@example.com", password="oldpass")

    send = raw_client.post(
        f"{API}/auth/password/reset/send", json={"username": "recover", "channel": "email"}
    ).json()
    code = send["dev_code"]

    conf = raw_client.post(
        f"{API}/auth/password/reset/confirm",
        json={"username": "recover", "channel": "email", "code": code, "new_password": "brandnew1"},
    )
    assert conf.status_code == 200 and conf.json()["reset"] is True

    # 新密码可登录，旧密码不行
    ok = raw_client.post(f"{API}/auth/login", json={"username": "recover", "password": "brandnew1"})
    assert ok.status_code == 200
    bad = raw_client.post(f"{API}/auth/login", json={"username": "recover", "password": "oldpass"})
    assert bad.status_code == 401


def test_reset_wrong_code_fails(raw_client, db):
    _make_verified_user(db, "recover2", "recover2@example.com", password="oldpass")
    raw_client.post(
        f"{API}/auth/password/reset/send", json={"username": "recover2", "channel": "email"}
    )
    conf = raw_client.post(
        f"{API}/auth/password/reset/confirm",
        json={"username": "recover2", "channel": "email", "code": "000000", "new_password": "brandnew1"},
    )
    assert conf.status_code == 400


def test_reset_code_not_reusable_for_verify(raw_client, db):
    """purpose 隔离：找回码不能被拿去当验证联系方式用（反之亦然由 verification 侧保证）。"""
    _make_verified_user(db, "recover3", "recover3@example.com", password="oldpass")
    send = raw_client.post(
        f"{API}/auth/password/reset/send", json={"username": "recover3", "channel": "email"}
    ).json()
    # 用找回码去走「验证联系方式」的 confirm，应当失败（purpose 不匹配）
    verify = raw_client.post(
        f"{API}/auth/verification/confirm",
        json={"channel": "email", "target": "recover3@example.com",
              "code": send["dev_code"], "username": "recover3", "password": "oldpass"},
    )
    assert verify.status_code == 400


def test_reset_confirm_does_not_grant_verification(db):
    """purpose="reset" 的确认只消费验证码，不置位 verified。

    否则一次改密会悄悄撤销掉管理员此前的「撤销验证」——被撤销验证的账号本应被登录闸门挡住。
    """
    u = User(username="revoke", email="revoke@example.com", email_verified=False,
             password_hash=hash_password("123456"))
    db.add(u)
    db.commit()
    db.refresh(u)
    code = request_code(db, u, "email", "revoke@example.com", purpose="reset")
    confirm_code(db, u, "email", "revoke@example.com", code, purpose="reset")
    db.refresh(u)
    assert u.email_verified is False


def test_change_password_requires_correct_old(client, raw_client, db):
    # current_user = tester / 123456；原密码错 -> 400，且原密码仍可用
    r = client.patch(
        f"{API}/auth/me/password", json={"old_password": "wrong", "new_password": "newpass123"}
    )
    assert r.status_code == 400
    ok = raw_client.post(f"{API}/auth/login", json={"username": "tester", "password": "123456"})
    assert ok.status_code == 200


def test_change_password_success(client, raw_client, db):
    r = client.patch(
        f"{API}/auth/me/password", json={"old_password": "123456", "new_password": "newpass123"}
    )
    assert r.status_code == 200 and r.json()["changed"] is True
    ok = raw_client.post(f"{API}/auth/login", json={"username": "tester", "password": "newpass123"})
    assert ok.status_code == 200
    bad = raw_client.post(f"{API}/auth/login", json={"username": "tester", "password": "123456"})
    assert bad.status_code == 401
