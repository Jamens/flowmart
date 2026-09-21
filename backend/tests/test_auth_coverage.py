"""鉴权覆盖测试：任何业务端点都必须挂鉴权依赖。

为什么需要这一层：过去漏鉴权是靠「改哪个文件顺便看一眼」发现的，结果连续漏掉
商品上下架、分类增删改查、流程定义创建/修改/归档、admin_db 数据接口等一整片，
其中「未登录即可改/删流程定义」会直接威胁订单状态机。

这里不再靠人肉检查，而是用程序枚举所有端点，把「是否挂了
get_current_user / require_admin」变成一条自动化断言 ——
新增端点忘记鉴权会立刻在 CI 里失败。
"""
from app.api import (  # noqa: E402
    admin_db,
    auth,
    cart,
    categories,
    orders,
    products,
    users,
    workflows,
)
from app.core.security import get_current_user, require_admin  # noqa: E402

API = "/api/v1"

# 按设计公开的端点：注册/登录/退出本身不可能要求已登录
PUBLIC = {f"{API}/auth/login", f"{API}/auth/register", f"{API}/auth/logout"}

MODULES = [auth, products, categories, cart, users, orders, workflows, admin_db]

AUTH_CALLS = {get_current_user: "get_current_user", require_admin: "require_admin"}


def _collect(dep, found):
    """递归收集一个端点上挂的所有鉴权依赖。"""
    for sub in getattr(dep, "dependencies", []) or []:
        _collect(sub, found)
    call = getattr(dep, "call", None)
    if call in AUTH_CALLS:
        found.add(AUTH_CALLS[call])


def _all_endpoints():
    """返回 [(method, path, {鉴权依赖...})]。

    不遍历 app.routes：新版 FastAPI 把 include_router 留成 _IncludedRouter 包装
    对象、不再把子路由展平，直接遍历会漏掉全部业务接口。
    """
    rows = []
    for mod in MODULES:
        for r in mod.router.routes:
            methods = getattr(r, "methods", None)
            if not methods:
                continue
            found = set()
            _collect(r.dependant, found)
            for m in sorted(methods):
                if m == "HEAD":
                    continue
                rows.append((m, API + r.path, found))
    return rows


def test_every_endpoint_requires_auth():
    missing = [
        f"{m} {path}"
        for m, path, found in _all_endpoints()
        if path not in PUBLIC and not found
    ]
    assert not missing, (
        "以下端点没有挂任何鉴权依赖（应加 get_current_user 或 require_admin）："
        f"{missing}"
    )


def test_public_endpoints_stay_public():
    """反向断言：认证三件套必须保持公开，防止有人误加鉴权导致无法登录。"""
    for m, path, found in _all_endpoints():
        if path in PUBLIC:
            assert not found, f"{m} {path} 是登录入口，不应要求鉴权"


def test_auth_declaration_matches_expectation():
    """管理员闸门必须建立在身份验证之上（require_admin 内含 get_current_user）。"""
    for m, path, found in _all_endpoints():
        if "require_admin" in found:
            assert "get_current_user" in found, (
                f"{m} {path} 用了 require_admin 却没有 get_current_user，"
                "身份未经验证就判角色是不安全的"
            )
