"""登录失败限流：可插拔后端的固定窗口计数器。

为什么是固定窗口 + 可插拔存储：
- **单实例**（开发、小流量）：用进程内内存 dict 即可，零外部依赖；
- **多实例 / 负载均衡**：必须换成共享存储（Redis），否则每个进程各计各的、
  攻击者把请求打散到不同实例就能绕过限流。后端抽象成 `RateLimitStore`，
  对外接口不变，`MemoryStore` / `RedisStore` 任意切换（由 `LOGIN_RATE_LIMIT_REDIS_URL` 决定）。

限流键为什么是 (IP, 用户名) 而非纯 IP：
- 纯 IP 限流会把「同一出口 IP 下的所有正常用户」一起误伤（办公网 NAT 场景）；
- (IP, 用户名) 更贴近「针对某个账号的暴力破解」，代价是理论上可用大量不同用户名
  对单个 IP 做「账号预封锁」DoS——本项目以「防密码爆破」为主，故取该权衡。

**⚠️ 客户端 IP 来源（部署前提）**：
`_client_ip` 默认取 `request.client.host`（直连真实 socket 地址，客户端无法伪造），
仅当 `LOGIN_RATE_LIMIT_TRUST_PROXY=True` 时才信任 `X-Forwarded-For` 首跳。
`X-Forwarded-For` 只有在反向代理（Nginx 等）已用真实客户端 IP **覆写**该头时才可信；
若直连或客户端可控该头却开了 `TRUST_PROXY=True`，攻击者可每次伪造不同 XFF 生成新限流键、
永远累计不到阈值——限流直接失效。详见 `app/core/config.py` 对应字段注释。
"""
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, Request

from app.core.config import settings
from app.core.security import get_current_user


@dataclass
class _Bucket:
    window_start: float
    failures: int


class RateLimitStore(ABC):
    """限流计数后端接口。两种实现：内存（单实例）/ Redis（多实例共享）。"""

    @abstractmethod
    def register_failure(self, key: str, window: int) -> int:
        """记一次失败，返回当前窗口内失败总数。"""

    @abstractmethod
    def count(self, key: str, window: int) -> int:
        """当前窗口内失败计数（无键/已过期返回 0）。"""

    @abstractmethod
    def ttl(self, key: str, window: int) -> int:
        """距窗口重置剩余秒数（无键/无过期返回 window）。"""

    @abstractmethod
    def reset(self, key: str) -> None:
        """清空单个键（登录成功）。"""

    @abstractmethod
    def reset_all(self) -> None:
        """清空全部计数（测试隔离 / 运维解封）。"""

    def register_hit(self, key: str, window: int) -> int:
        """记一次「请求命中」（通用限流），返回当前窗口内总数。

        实现与 register_failure 完全相同（都是窗口内 +1），分开命名只为可读性：
        登录限流只关心**失败**，通用限流关心**每一次调用**——成功请求也算数。
        """
        return self.register_failure(key, window)


class MemoryStore(RateLimitStore):
    """进程内固定窗口计数。单实例够用；非原子跨进程。"""

    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}
        # login 是 sync 端点，由 FastAPI 在线程池里跑，并发请求会同时改 _buckets；
        # read-modify-write（failures += 1）与 is_blocked/register_failure 之间的 TOCTOU
        # 都要锁兜住，否则计数可能丢失（最坏多放行几次，不会崩，但削弱限流）。
        self._lock = threading.Lock()

    def _current(self, key: str, window: int) -> _Bucket | None:
        b = self._buckets.get(key)
        if b is None:
            return None
        if time.monotonic() - b.window_start >= window:
            self._buckets.pop(key, None)  # 窗口过期：清旧计数，当全新开始
            return None
        return b

    def register_failure(self, key: str, window: int) -> int:
        with self._lock:
            b = self._current(key, window)
            if b is None:
                b = _Bucket(window_start=time.monotonic(), failures=0)
                self._buckets[key] = b
            b.failures += 1
            return b.failures

    def count(self, key: str, window: int) -> int:
        with self._lock:
            b = self._current(key, window)
            return b.failures if b else 0

    def ttl(self, key: str, window: int) -> int:
        with self._lock:
            b = self._current(key, window)
            if b is None:
                return window
            remaining = window - (time.monotonic() - b.window_start)
            return max(1, int(remaining))

    def reset(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)

    def reset_all(self) -> None:
        with self._lock:
            self._buckets.clear()


class RedisStore(RateLimitStore):
    """Redis 后端：多实例共享计数。

    固定窗口用 `INCR` + `EXPIRE` 实现：首次失败 INCR 返回 1 时设过期（窗口秒），
    之后累加计数的 TTL 不再刷新——即「首失败时刻 + 窗口」为一个窗口，符合固定窗口语义。
    窗口到期由 Redis 自动删键，故 `count`/`ttl` 无需自行判断过期。

    仅当 `LOGIN_RATE_LIMIT_REDIS_URL` 非空才构建；redis 延迟导入，避免单实例也强依赖 redis-py。
    """

    PREFIX = "flowmart:rl:"

    def __init__(self, url: str | None = None, client: Any = None) -> None:
        if client is not None:
            self._r = client  # 测试注入（fakeredis 等），不连真服务
        else:
            import redis  # 延迟导入：仅启用 Redis 后端时才需要

            self._r = redis.Redis.from_url(url, socket_timeout=2)
            # 启动时探活：连不上直接报错，避免上线才发现限流失效（fail-fast）
            try:
                self._r.ping()
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"登录限流 Redis 后端连不上（{url}）：{exc}。"
                    "多实例部署需 Redis 共享计数；单实例可留空 LOGIN_RATE_LIMIT_REDIS_URL 用内存实现。"
                ) from exc

    def _k(self, key: str) -> str:
        return f"{self.PREFIX}{key}"

    def register_failure(self, key: str, window: int) -> int:
        k = self._k(key)
        # SET NX EX 先确保键存在且带窗口过期（仅首次设置成功，已存在则不变 → 不刷新 TTL），
        # 再 INCR 累加。这样没有「INCR 成功但 expire 前进程崩溃留下无 TTL 键→永久封禁」的窗口，
        # 且 TTL 锚定在首失败时刻，符合固定窗口语义。INCR 原子，并发安全。
        self._r.set(k, 0, nx=True, ex=window)
        return self._r.incr(k)

    def count(self, key: str, window: int) -> int:
        v = self._r.get(self._k(key))
        return int(v) if v is not None else 0

    def ttl(self, key: str, window: int) -> int:
        t = self._r.ttl(self._k(key))
        # t == -2 键不存在；t == -1 无过期（正常不会触发，因首失败必设 expire）
        if t is None or t < 0:
            return window
        return max(1, t)

    def reset(self, key: str) -> None:
        self._r.delete(self._k(key))

    def reset_all(self) -> None:
        # 只清本服务前缀，避免误删其它 key
        for k in self._r.scan_iter(match=f"{self.PREFIX}*"):
            self._r.delete(k)


_STORE: RateLimitStore | None = None


def _build_store() -> RateLimitStore:
    """共享的单例后端：登录限流与通用限流**共用一个** store。

    刻意共享（而不是各建一个）有两个原因：
    1. 两个 RedisStore 会各建一条连接、启动时各 ping 一次，纯浪费；
    2. 更关键的是 `reset_all()` 的语义必须与后端无关：
       RedisStore.reset_all 按 `flowmart:rl:` 前缀扫描，会**连同另一个限流器的
       计数一起清掉**；而两个独立 MemoryStore 只会清自己。不共享的话，
       同一个「解封」操作在内存/Redis 两种后端下行为不一致，
       开发环境还根本复现不出来——上线才发现解封把全站限流清了。
    """
    global _STORE
    if _STORE is None:
        url = settings.LOGIN_RATE_LIMIT_REDIS_URL
        _STORE = RedisStore(url) if url else MemoryStore()
    return _STORE


class LoginRateLimiter:
    """按 (客户端IP, 用户名) 固定窗口计数登录失败次数。"""

    def __init__(self) -> None:
        # 限流状态必须在进程内跨请求共享（或跨实例经 Redis 共享）才有效
        self._store = _build_store()

    @staticmethod
    def _client_ip(request) -> str:
        # 安全默认：直连取 socket 地址（客户端无法伪造）。
        # 仅当反向代理已用真实客户端 IP 覆写 X-Forwarded-For 且显式开启
        # LOGIN_RATE_LIMIT_TRUST_PROXY 时，才信任 XFF 首跳——否则攻击者每次伪造不同
        # XFF 即可绕过限流（code review P1）。
        if settings.LOGIN_RATE_LIMIT_TRUST_PROXY:
            forwarded = request.headers.get("x-forwarded-for")
            if forwarded:
                return forwarded.split(",")[0].strip()
        client = getattr(request, "client", None)
        return client.host if client else "unknown"

    def _key(self, request, username: str) -> str:
        # 带 login: 命名空间：通用限流的键是 `{scope}:{ip}`，两者共用同一个 store，
        # 不带前缀时伪造的 XFF 可能让两个键撞在一起（互相投毒配额）。
        return f"login:{self._client_ip(request)}:{username}"

    def register_failure(self, request, username: str) -> None:
        self._store.register_failure(self._key(request, username), settings.LOGIN_RATE_LIMIT_WINDOW)

    def is_blocked(self, request, username: str) -> bool:
        return (
            self._store.count(self._key(request, username), settings.LOGIN_RATE_LIMIT_WINDOW)
            >= settings.LOGIN_RATE_LIMIT_MAX
        )

    def retry_after(self, request, username: str) -> int:
        return self._store.ttl(self._key(request, username), settings.LOGIN_RATE_LIMIT_WINDOW)

    def reset(self, request, username: str) -> None:
        """登录成功清空计数，避免正常用户刚改完密码就被旧失败数误伤。"""
        self._store.reset(self._key(request, username))

    def reset_all(self) -> None:
        self._store.reset_all()


# 模块级单例：限流状态必须在进程内跨请求共享才有效。
login_limiter = LoginRateLimiter()


class RateLimiter:
    """通用固定窗口限流：按「客户端 IP + 业务 scope」计数**每次请求**。

    与 LoginRateLimiter 的分工：
    - 后者只记失败、键带用户名，目标是防「针对某个账号的密码爆破」；
    - 这里记全部请求、键只带 IP + scope，目标是防「灌账号 / 验证码轰炸 /
      找回密码轰炸」这类**不看成败、只看频率**的滥用——成功调用同样消耗配额，
      否则攻击者用正确参数高频调用就能把短信/邮件渠道打爆。

    IP 取值策略刻意与登录限流完全一致（默认 socket 地址，XFF 需显式开信任），
    避免两套限流对「客户端是谁」判断不一致而被绕过。
    """

    def __init__(self) -> None:
        self._store = _build_store()

    @staticmethod
    def _client_ip(request: Request) -> str:
        return LoginRateLimiter._client_ip(request)

    def _key(self, request: Request, scope: str, identity: str | None = None) -> str:
        # gen: 命名空间，与登录限流的 login: 前缀隔离（见 LoginRateLimiter._key 注释）
        # identity 优先：已登录端点按身份计数，公共端点才退回 IP
        return f"gen:{scope}:{identity or self._client_ip(request)}"

    def hit(
        self,
        request: Request,
        scope: str,
        limit: int,
        window: int,
        identity: str | None = None,
    ) -> int:
        """记一次请求：返回 0 表示未超限，否则返回建议的 Retry-After 秒数。

        identity 非空时按身份计数（已认证端点），否则按客户端 IP（公共端点）。
        """
        key = self._key(request, scope, identity)
        count = self._store.register_hit(key, window)
        if count > limit:
            return self._store.ttl(key, window)
        return 0

    def reset_all(self) -> None:
        self._store.reset_all()


# 模块级单例：同上，状态必须跨请求共享。
limiter = RateLimiter()


def rate_limit(scope: str):
    """生成 FastAPI 依赖：按 (IP, scope) 限流，超限返回 429 + Retry-After。

    阈值在**请求时**按 scope 从 settings 现读（`RATE_LIMIT_{SCOPE}_MAX`），
    刻意在装饰器求值期固化：否则测试无法 monkeypatch 阈值，只能靠「真的打满
    配额」来测——既慢，又会让断言和默认阈值绑死。0 或负 = 关闭该 scope。

    做成**按端点 opt-in** 而非全局中间件：一刀切会误伤列表/详情这类高频只读
    接口，也会让测试套件因为「请求太多」而随机失败。
    """

    def _dep(request: Request) -> None:
        limit = getattr(settings, f"RATE_LIMIT_{scope.upper()}_MAX", 0)
        if limit <= 0:
            return
        retry = limiter.hit(request, scope, limit, settings.RATE_LIMIT_WINDOW)
        if retry:
            raise HTTPException(
                status_code=429,
                detail="请求过于频繁，请稍后再试",
                headers={"Retry-After": str(retry)},
            )

    return _dep


def rate_limit_user(scope: str):
    """已认证端点的限流：按 **user_id** 计数，而不是 IP。

    为什么这类端点不能用 IP 做维度：
    - NAT / CGNAT 下同一出口 IP 会误伤一片正常用户（办公网、移动网络）；
    - 反过来 IPv6 / 代理下攻击者又能随手换 IP，IP 维度形同虚设。
    已登录接口本来就有稳定身份，就该按身份限。

    典型场景是下单 / 结算：`create_order` 会**原子扣库存**，刷单可以把库存
    打到 0 —— 这是业务型 DoS，比打爆 CPU 更难恢复。
    """

    def _dep(request: Request, current_user=Depends(get_current_user)) -> None:
        limit = getattr(settings, f"RATE_LIMIT_{scope.upper()}_MAX", 0)
        if limit <= 0:
            return
        retry = limiter.hit(
            request, scope, limit, settings.RATE_LIMIT_WINDOW,
            identity=f"user:{current_user.id}",
        )
        if retry:
            raise HTTPException(
                status_code=429,
                detail="请求过于频繁，请稍后再试",
                headers={"Retry-After": str(retry)},
            )

    return _dep
