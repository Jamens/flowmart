"""登录限流（防暴力破解）：连续失败达阈值返 429 + Retry-After；成功清空计数；
不同 (IP, 用户名) 独立计数；窗口过期后恢复。

用 raw_client（不覆盖鉴权）跑真实登录链路。

部署加固相关的测试：
- 默认（LOGIN_RATE_LIMIT_TRUST_PROXY=False）只取 request.client.host，X-Forwarded-For
  即使被伪造也**不会**生成新限流键 → 防绕过；
- 开启 TRUST_PROXY 后 XFF 首跳才生效，可模拟不同客户端 IP；
- Redis 后端（多实例共享计数）用 fakeredis 离线验证，无需真 Redis 服务。
"""
import time

import pytest

from app.core.config import settings
from app.core.ratelimit import LoginRateLimiter, MemoryStore, RedisStore, login_limiter


@pytest.fixture(autouse=True)
def _clear_limiter():
    # 模块级单例跨用例共享，清空避免计数泄漏造成误伤/误判
    login_limiter.reset_all()
    yield
    login_limiter.reset_all()


def _register(raw_client, username, password="secret1"):
    raw_client.post("/api/v1/auth/register", json={"username": username, "password": password})


def _verify(raw_client, token, target="verified@example.com", channel="email"):
    """测试夹具：用令牌走 OTP 验证，便于后续登录通过登录验证闸门。"""
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
    reg = raw_client.post("/api/v1/auth/register", json={"username": "rl_reset", "password": "secret1"}).json()
    _verify(raw_client, reg["access_token"])
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


def test_distinct_ip_independent(raw_client, monkeypatch):
    """限流键含 IP：同一用户名从不同 IP 登录互不干扰（需开启 TRUST_PROXY 才信 XFF）。"""
    monkeypatch.setattr(settings, "LOGIN_RATE_LIMIT_TRUST_PROXY", True)
    reg = raw_client.post("/api/v1/auth/register", json={"username": "rl_ip", "password": "secret1"}).json()
    _verify(raw_client, reg["access_token"])
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
    reg = raw_client.post("/api/v1/auth/register", json={"username": "rl_expire", "password": "secret1"}).json()
    _verify(raw_client, reg["access_token"])
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


def test_xff_ignored_when_proxy_untrusted(raw_client):
    """默认不信任代理：伪造 X-Forwarded-For 无法生成新限流键，绕过限流被封堵。

    这是 code review 指出的关键运营风险——若直连场景也信 XFF，攻击者可每次换 IP
    永远累计不到阈值。默认取 request.client.host（无法伪造）即关闭该绕过。
    """
    _register(raw_client, "rl_xff")
    # 用 XFF=1.1.1.1 打满失败（TRUST_PROXY 默认 False，XFF 被忽略，键仍是 testclient:rl_xff）
    for _ in range(settings.LOGIN_RATE_LIMIT_MAX):
        raw_client.post(
            "/api/v1/auth/login",
            json={"username": "rl_xff", "password": "wrong"},
            headers={"X-Forwarded-For": "1.1.1.1"},
        )
    # 再换一个伪造 XFF=9.9.9.9 尝试绕过 → 仍同一键，依旧被限流（429 而非 401）
    r = raw_client.post(
        "/api/v1/auth/login",
        json={"username": "rl_xff", "password": "wrong"},
        headers={"X-Forwarded-For": "9.9.9.9"},
    )
    assert r.status_code == 429


def test_redis_store_fixed_window_semantics():
    """Redis 后端固定窗口语义（fakeredis 离线跑真实 redis-py 命令）。"""
    fakeredis = pytest.importorskip("fakeredis")
    store = RedisStore(client=fakeredis.FakeStrictRedis())
    store.reset_all()
    key = "1.2.3.4:alice"
    assert store.register_failure(key, 60) == 1
    assert store.register_failure(key, 60) == 2
    assert store.count(key, 60) == 2
    assert store.ttl(key, 60) > 0  # 窗口内仍有剩余时间
    # 成功登录清空单个键
    store.reset(key)
    assert store.count(key, 60) == 0
    # 窗口过期后 count 归零、ttl 回退到 window（Redis 自动删键）
    store.register_failure(key, 60)
    store._r.expire(store._k(key), 0)  # 立即使键过期
    assert store.count(key, 60) == 0
    assert store.ttl(key, 60) == 60


def test_limiter_redis_backend_end_to_end(raw_client, monkeypatch):
    """全链路：单例切换为 Redis 后端后，阈值封禁 + 成功重置仍正确（多实例共享计数）。"""
    fakeredis = pytest.importorskip("fakeredis")
    import redis as _redis

    fake = fakeredis.FakeStrictRedis()
    # 让 RedisStore 用 fakeredis 而非真服务（仅本测试生效）
    monkeypatch.setattr(_redis.Redis, "from_url", lambda *a, **k: fake)
    monkeypatch.setattr(settings, "LOGIN_RATE_LIMIT_REDIS_URL", "redis://fake:6379/0")
    # 重建单例为 Redis 后端
    import app.core.ratelimit as rl_mod

    rl_mod.login_limiter = LoginRateLimiter()
    try:
        reg = raw_client.post("/api/v1/auth/register", json={"username": "rl_redis", "password": "secret1"}).json()
        _verify(raw_client, reg["access_token"])
        # 失败到「差一次阈值」（尚未被限流），验证 Redis 后端计数生效
        for _ in range(settings.LOGIN_RATE_LIMIT_MAX - 1):
            assert (
                raw_client.post(
                    "/api/v1/auth/login", json={"username": "rl_redis", "password": "wrong"}
                ).status_code
                == 401
            )
        # 成功登录 → 经 Redis 清空计数（证明 Redis 后端的 reset 路径）
        assert (
            raw_client.post(
                "/api/v1/auth/login", json={"username": "rl_redis", "password": "secret1"}
            ).status_code
            == 200
        )
        # 计数已清：再失败一次不应被限流（仍是 401 而非 429）
        assert (
            raw_client.post(
                "/api/v1/auth/login", json={"username": "rl_redis", "password": "wrong"}
            ).status_code
            == 401
        )
    finally:
        rl_mod.login_limiter.reset_all()
        # 直接还原为内存后端（而非重建单例）：此时 URL monkeypatch 仍生效，
        # 重建会得到仍是 RedisStore(fake)，导致单例在后续测试里仍走 Redis 后端。
        rl_mod.login_limiter._store = MemoryStore()
