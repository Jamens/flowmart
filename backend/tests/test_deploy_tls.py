"""域名 / 部署环境 / TLS 可配置项的测试。

开源项目不能替使用者决定部署形态，所以这些开关必须：配错启动即报错、
不配就用安全默认、且行为可测。测试构造 Settings 时统一 _env_file=None，
避免本机 .env（含端到端验收用的放行开关）悄悄改变断言结果。
"""
import asyncio

import pytest
from starlette.requests import Request
from starlette.responses import Response

import app.core.config as config_module
from app.core.config import Settings
from app.core.security import set_auth_cookie
from app.main import _tls_enforcement


def _settings(**kw):
    return Settings(_env_file=None, **kw)


# --------------------------------------------------------------------------- #
# 配置校验（fail-fast）
# --------------------------------------------------------------------------- #
def test_deploy_env_must_be_known():
    with pytest.raises(ValueError, match="DEPLOY_ENV"):
        _settings(DEPLOY_ENV="prod")


def test_production_implies_non_debug():
    """production 是明确的「线上」标记，绝不能和调试模式同时出现。"""
    with pytest.raises(ValueError, match="production"):
        _settings(DEPLOY_ENV="production", DEBUG=True)


def test_public_base_url_must_be_https():
    with pytest.raises(ValueError, match="PUBLIC_BASE_URL"):
        _settings(PUBLIC_BASE_URL="http://shop.example.com")


def test_enforce_https_requires_base_url_and_proxy():
    with pytest.raises(ValueError, match="PUBLIC_BASE_URL"):
        _settings(ENFORCE_HTTPS=True, TRUST_PROXY=True)
    with pytest.raises(ValueError, match="TRUST_PROXY"):
        _settings(ENFORCE_HTTPS=True, PUBLIC_BASE_URL="https://shop.example.com")


def test_hsts_max_age_non_negative():
    with pytest.raises(ValueError, match="HSTS_MAX_AGE"):
        _settings(HSTS_MAX_AGE=-1)


def test_payment_notify_falls_back_to_public_base_url():
    """支付回调地址留空时回退到 PUBLIC_BASE_URL：域名只配一次就够了。"""
    s = _settings(
        PAYMENT_PROVIDER="wechat",
        PUBLIC_BASE_URL="https://shop.example.com",
        WECHAT_APPID="a",
        WECHAT_MCHID="m",
        WECHAT_API_V3_KEY="k" * 32,
        WECHAT_MCH_CERT_SERIAL_NO="s",
        WECHAT_PRIVATE_KEY_PATH="/tmp/k.pem",
        WECHAT_PLATFORM_CERT_PATH="/tmp/c.pem",
    )
    assert s.PAYMENT_NOTIFY_BASE_URL == "https://shop.example.com"


def test_defaults_construct():
    """全默认（dev / 同源直跑）必须能正常构造。"""
    s = _settings()
    assert s.DEPLOY_ENV == "dev"
    assert s.TRUST_PROXY is False
    assert s.ENFORCE_HTTPS is False


# --------------------------------------------------------------------------- #
# 行为：HTTPS 强制跳转 + HSTS + Cookie 域
# --------------------------------------------------------------------------- #
def _make_request(scheme="https", proto_header=None, path="/somepath"):
    """构造 Starlette Request。

    注意：Starlette 的 TestClient 把 scope scheme 恒设为 https，无法用它测 http 分支，
    所以直接构造 request 对象并调用中间件协程本身（用 asyncio.run 驱动）。
    """
    headers = [(b"x-forwarded-proto", proto_header.encode())] if proto_header else []
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": b"",
        "headers": headers,
        "server": ("testserver", 80),
        "scheme": scheme,
    }
    return Request(scope)


async def _noop_call_next(request):
    return Response("ok")


def test_http_request_redirected_to_https(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ENFORCE_HTTPS", True)
    monkeypatch.setattr(config_module.settings, "TRUST_PROXY", True)
    monkeypatch.setattr(config_module.settings, "PUBLIC_BASE_URL", "https://shop.example.com")
    resp = asyncio.run(_tls_enforcement(_make_request(scheme="http"), _noop_call_next))
    assert resp.status_code == 307
    assert resp.headers["location"].startswith("https://testserver/somepath")


def test_https_request_gets_hsts(monkeypatch):
    monkeypatch.setattr(config_module.settings, "ENFORCE_HTTPS", True)
    monkeypatch.setattr(config_module.settings, "TRUST_PROXY", True)
    monkeypatch.setattr(config_module.settings, "PUBLIC_BASE_URL", "https://shop.example.com")
    monkeypatch.setattr(config_module.settings, "HSTS_MAX_AGE", 31536000)
    resp = asyncio.run(_tls_enforcement(_make_request(scheme="https"), _noop_call_next))
    assert resp.status_code == 200
    assert resp.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"


def test_health_skips_redirect(monkeypatch):
    """健康检查 /health 即便强制 HTTPS 也不跳：反代内部用 http 探活，跳了反而误判失败。"""
    monkeypatch.setattr(config_module.settings, "ENFORCE_HTTPS", True)
    monkeypatch.setattr(config_module.settings, "TRUST_PROXY", True)
    monkeypatch.setattr(config_module.settings, "PUBLIC_BASE_URL", "https://shop.example.com")
    resp = asyncio.run(
        _tls_enforcement(_make_request(scheme="http", path="/health"), _noop_call_next)
    )
    assert resp.status_code == 200


def test_trusted_proxy_proto_suppresses_redirect(monkeypatch):
    """反代已用 X-Forwarded-Proto 声明真实协议为 https 时，外部 http 请求不应被跳。"""
    monkeypatch.setattr(config_module.settings, "ENFORCE_HTTPS", True)
    monkeypatch.setattr(config_module.settings, "TRUST_PROXY", True)
    monkeypatch.setattr(config_module.settings, "PUBLIC_BASE_URL", "https://shop.example.com")
    req = _make_request(scheme="http", proto_header="https")
    resp = asyncio.run(_tls_enforcement(req, _noop_call_next))
    assert resp.status_code == 200


def test_untrusted_proxy_proto_does_not_spoof_https(monkeypatch):
    """不开 TRUST_PROXY 时，攻击者自己塞 X-Forwarded-Proto: https 不能骗过判定。"""
    monkeypatch.setattr(config_module.settings, "ENFORCE_HTTPS", True)
    monkeypatch.setattr(config_module.settings, "TRUST_PROXY", False)
    monkeypatch.setattr(config_module.settings, "PUBLIC_BASE_URL", "https://shop.example.com")
    req = _make_request(scheme="http", proto_header="https")
    resp = asyncio.run(_tls_enforcement(req, _noop_call_next))
    assert resp.status_code == 307  # 照样跳 https


def test_cookie_domain_set_when_configured(monkeypatch):
    """前后端分域部署时，令牌 Cookie 要带 Domain=.example.com 才能跨子域携带。"""
    monkeypatch.setattr(config_module.settings, "COOKIE_DOMAIN", ".example.com")
    resp = Response()
    set_auth_cookie(resp, "tok")
    assert "Domain=.example.com" in resp.headers["set-cookie"]


def test_cookie_domain_omitted_by_default(monkeypatch):
    monkeypatch.setattr(config_module.settings, "COOKIE_DOMAIN", "")
    resp = Response()
    set_auth_cookie(resp, "tok")
    assert "Domain=" not in resp.headers["set-cookie"]
