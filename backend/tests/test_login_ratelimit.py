"""登录限流（防暴力破解）：连续失败达阈值返 429 + Retry-After；成功清空计数；
不同 (IP, 用户名) 独立计数；窗口过期后恢复。

用 raw_client（不覆盖鉴权）跑真实登录链路；不同 IP 用 X-Forwarded-For 头模拟，
避免 TestClient 固定 client host 导致所有用例撞同一 IP。
"""
import time

import pytest

from app.core.config import settings
from app.core.ratelimit import login_limiter


@pytest.fixture(autouse=True)
def _clear_limiter():
    # 模块级单例跨用例共享，清空避免计数泄漏造成误伤/误判
    login_limiter.reset_all()
    yield
    login_limiter.reset_all()


def _register(raw_client, username, password="secret1"):
    raw_client.post("/api/v1/auth/register", json={"username": username, "password": password})


def test_rate_limit_blocks_after_threshold(raw_client):
    """同一 (IP, 用户名) 失败达阈值 → 429 且带 Retry-After。"""
    _register(raw_client, "rl_victim")
    # 阈值内的失败都是 401
    for _ in range(settings.LOGIN_RATE_LIMIT_MAX):
        r = raw_client.post(
            "/api/v1/auth/login", json={"username": "rl_victim", "password": "wrong"}
        )
        assert r.status_code == 401
    # 再试一次应被限流
    r = raw_client.post(
        "/api/v1/auth/login", json={"username": "rl_victim", "password": "wrong"}
    )
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert int(r.headers["Retry-After"]) > 0


def test_successful_login_resets_failures(raw_client):
    """登录成功清空失败计数，正常用户不会被旧失败数误伤。"""
    _register(raw_client, "rl_reset")
    # 失败到阈值差一次（未达限流）
    for _ in range(settings.LOGIN_RATE_LIMIT_MAX - 1):
        r = raw_client.post(
            "/api/v1/auth/login", json={"username": "rl_reset", "password": "wrong"}
        )
        assert r.status_code == 401
    # 成功登录 → 重置计数
    r = raw_client.post(
        "/api/v1/auth/login", json={"username": "rl_reset", "password": "secret1"}
    )
    assert r.status_code == 200
    # 之后又失败一次，因计数已清，不应被限流（仍是 401 而非 429）
    r = raw_client.post(
        "/api/v1/auth/login", json={"username": "rl_reset", "password": "wrong"}
    )
    assert r.status_code == 401


def test_distinct_ip_independent(raw_client):
    """限流键含 IP：同一用户名从不同 IP 登录互不干扰。"""
    _register(raw_client, "rl_ip")
    # 从 IP 1.1.1.1 打满失败
    for _ in range(settings.LOGIN_RATE_LIMIT_MAX):
        raw_client.post(
            "/api/v1/auth/login",
            json={"username": "rl_ip", "password": "wrong"},
            headers={"X-Forwarded-For": "1.1.1.1"},
        )
    # 同一用户名但从 IP 2.2.2.2 → 计数独立，正确密码仍可达 200
    r = raw_client.post(
        "/api/v1/auth/login",
        json={"username": "rl_ip", "password": "secret1"},
        headers={"X-Forwarded-For": "2.2.2.2"},
    )
    assert r.status_code == 200


def test_window_expiry_recovers(raw_client, monkeypatch):
    """窗口过期后限流解除，可再次登录（用假时钟避免真实 sleep 60s）。"""
    clock = {"t": 1000.0}
    monkeypatch.setattr("app.core.ratelimit.time.monotonic", lambda: clock["t"])
    _register(raw_client, "rl_expire")
    # 窗口内打满 → 被限流
    for _ in range(settings.LOGIN_RATE_LIMIT_MAX):
        raw_client.post(
            "/api/v1/auth/login", json={"username": "rl_expire", "password": "wrong"}
        )
    assert (
        raw_client.post(
            "/api/v1/auth/login",
            json={"username": "rl_expire", "password": "wrong"},
        ).status_code
        == 429
    )
    # 推进时间越过窗口
    clock["t"] += settings.LOGIN_RATE_LIMIT_WINDOW + 1
    r = raw_client.post(
        "/api/v1/auth/login", json={"username": "rl_expire", "password": "secret1"}
    )
    assert r.status_code == 200
