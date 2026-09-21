"""登录失败限流：内存级固定窗口计数器。

为什么是内存级、而不是 Redis：
- 单实例部署（开发、小流量服务）足够，且零外部依赖；
- 多实例 / 负载均衡场景必须换成 Redis 等共享存储，否则每个实例各计各的、
  攻击者只要打散请求到不同实例就能绕过。届时把本模块的 `_buckets` 换成
  Redis 的 `INCR + EX` / 有序集合即可，对外接口保持不变。

限流键为什么是 (IP, 用户名) 而非纯 IP：
- 纯 IP 限流会把「同一出口 IP 下的所有正常用户」一起误伤（办公网 NAT 场景）；
- (IP, 用户名) 更贴近「针对某个账号的暴力破解」，且攻击者也只能封锁自己正在猜的账号，
  代价是理论上可用大量不同用户名对单个 IP 做「账号预封锁」DoS——
  本项目的威胁模型以「防密码爆破」为主，故取该权衡，注释在此点明。

**⚠️ 部署前提（重要）**：`_client_ip` 取 `X-Forwarded-For` 的第一个值作为客户端 IP。
若反向代理（Nginx 等）不**覆盖**该头，攻击者可每次伪造不同 `X-Forwarded-For`，
从而对每个请求生成新的限流键、永远无法累计到阈值——限流直接失效。
因此本限流**仅在可信网关已用真实客户端 IP 覆写 `X-Forwarded-For` 时有效**；
纯直连（无代理、客户端可控该头）场景下需改为取 `request.client.host`。
"""
import threading
import time
from dataclasses import dataclass

from app.core.config import settings


@dataclass
class _Bucket:
    window_start: float
    failures: int


class LoginRateLimiter:
    """按 (客户端IP, 用户名) 固定窗口计数登录失败次数。"""

    def __init__(self) -> None:
        # key = (client_ip, username) -> 当前窗口的失败计数与窗口起点
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        # login 是 sync 端点，由 FastAPI 在线程池里跑，并发请求会同时改 _buckets；
        # read-modify-write（failures += 1）与 is_blocked/register_failure 之间的 TOCTOU
        # 都需要一把锁兜住，否则计数可能丢失（最坏多放行几次，不会崩，但削弱了限流）。
        self._lock = threading.Lock()

    @staticmethod
    def _client_ip(request) -> str:
        # 反向代理（Nginx 等）透传的真实客户端 IP 优先；否则回退到直连 socket 地址。
        # 见文件顶部「部署前提」：该头必须来自可信网关的覆写，否则可被伪造绕过限流。
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        client = getattr(request, "client", None)
        return client.host if client else "unknown"

    def _key(self, request, username: str) -> tuple[str, str]:
        return (self._client_ip(request), username)

    def _current(self, key) -> _Bucket | None:
        """返回当前有效窗口的 bucket；若窗口已过期则视为无（返回 None）。

        调用方需已持有 self._lock。
        """
        b = self._buckets.get(key)
        if b is None:
            return None
        if time.monotonic() - b.window_start >= settings.LOGIN_RATE_LIMIT_WINDOW:
            # 窗口过期：清掉旧计数，当作全新开始
            self._buckets.pop(key, None)
            return None
        return b

    def register_failure(self, request, username: str) -> None:
        """记一次登录失败；进入新窗口则重置计数起点。"""
        with self._lock:
            key = self._key(request, username)
            b = self._current(key)
            if b is None:
                b = _Bucket(window_start=time.monotonic(), failures=0)
                self._buckets[key] = b
            b.failures += 1

    def is_blocked(self, request, username: str) -> bool:
        with self._lock:
            b = self._current(self._key(request, username))
            return b is not None and b.failures >= settings.LOGIN_RATE_LIMIT_MAX

    def retry_after(self, request, username: str) -> int:
        """距离窗口重置还需多少秒（用于 429 的 Retry-After 头）。"""
        with self._lock:
            b = self._current(self._key(request, username))
            if b is None:
                return settings.LOGIN_RATE_LIMIT_WINDOW
            remaining = settings.LOGIN_RATE_LIMIT_WINDOW - (
                time.monotonic() - b.window_start
            )
            return max(1, int(remaining))

    def reset(self, request, username: str) -> None:
        """登录成功清空计数，避免正常用户刚改完密码就被旧失败数误伤。"""
        with self._lock:
            self._buckets.pop(self._key(request, username), None)

    def reset_all(self) -> None:
        """清空全部计数（测试隔离 / 运维手动解封用）。"""
        with self._lock:
            self._buckets.clear()


# 模块级单例：限流状态必须在进程内跨请求共享才有效。
login_limiter = LoginRateLimiter()
