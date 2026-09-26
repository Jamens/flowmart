"""flowmart 端到端验收（跑在真实 compose 栈上：MySQL + Redis + 生产形态后端）。

刻意用**生产形态**（DEBUG=false）跑，不靠开发期的便利开关：
- 验证码不会回传给前端 → 从 MySQL 里读出来再确认
- 演示密码不会回填 → 账号走注册流程，管理员用 SQL 提升

用法：先 `docker compose up -d --build` + init_db + seed，再跑本脚本。
"""
import os
import subprocess
import sys
import time

import httpx

HOST = os.getenv("E2E_HOST", "http://127.0.0.1:8000")
API = HOST + "/api/v1"

# 数据库口令**不写死在脚本里**：从环境取，缺失即退出。
# 用法：先 `cp .env.example .env` 改好口令，再 `set -a; . ./.env; set +a; python e2e_acceptance.py`
MYSQL_PW = os.getenv("MYSQL_ROOT_PASSWORD")
if not MYSQL_PW:
    sys.exit("缺少 MYSQL_ROOT_PASSWORD（请先配置 .env 并导入环境变量）")
MYSQL = ["docker", "compose", "exec", "-T", "mysql", "mysql", "-uroot",
         f"-p{MYSQL_PW}", "-N", "-B", "flowmart", "-e"]

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    if not ok:
        print(f"  ✗ {name}  {detail}")
    return bool(ok)


def sql(query):
    r = subprocess.run(MYSQL + [query], capture_output=True, text=True, timeout=60)
    return r.stdout.strip()


def last_code(user_id):
    return sql(f"SELECT code FROM verification_codes WHERE user_id={user_id} "
               f"ORDER BY id DESC LIMIT 1")


def main():
    c = httpx.Client(timeout=30)
    anon = httpx.Client(timeout=30)

    # ---------- 1. 健康检查 ----------
    r = c.get(HOST + "/health")
    check("健康检查返回 200", r.status_code == 200, r.text[:120])
    check("数据库方言为 mysql", r.json().get("dialect") == "mysql", str(r.json()))

    # ---------- 2. 注册 + 验证 + 登录（买家） ----------
    # 每次运行用唯一账号：重跑时不会撞上「用户名已存在」，也避免读到上一次的残留数据
    run_id = str(int(time.time()))
    buyer = f"e2e_buyer_{run_id}"
    pwd = "e2e-pass-123"
    buyer_phone = f"138{str(int(time.time()))[-8:]}"
    r = c.post(API + "/auth/register", json={"username": buyer, "password": pwd})
    check("注册买家成功", r.status_code == 201, r.text[:200])
    buyer_id = sql(f"SELECT id FROM users WHERE username='{buyer}'")

    r = c.post(API + "/auth/verification/send", json={
        "channel": "phone", "target": buyer_phone,
        "username": buyer, "password": pwd,
    })
    check("申请手机验证码成功", r.status_code == 200, r.text[:200])
    code = last_code(buyer_id)
    check("验证码已落库（生产下不回传给前端）",
          bool(code) and "dev_code" not in r.json(), f"code={code}")

    r = c.post(API + "/auth/verification/confirm", json={
        "channel": "phone", "target": buyer_phone, "code": code,
        "username": buyer, "password": pwd,
    })
    check("确认验证码成功并置 phone_verified",
          r.status_code == 200 and r.json().get("phone_verified") is True, r.text[:200])

    r = c.post(API + "/auth/login", json={"username": buyer, "password": pwd})
    check("买家登录成功", r.status_code == 200, r.text[:200])
    buyer_token = r.json().get("access_token", "")
    bh = {"Authorization": f"Bearer {buyer_token}"}

    r = c.get(API + "/auth/me", headers=bh)
    check("/auth/me 显示非管理员",
          r.status_code == 200 and r.json().get("is_admin") is False, r.text[:200])

    # ---------- 3. 买家越权一律被拒 ----------
    for name, method, path in [
        ("买家访问统计接口 → 403", "GET", "/stats"),
        ("买家访问统计趋势 → 403", "GET", "/stats/trend"),
        ("买家创建商品 → 403", "POST", "/products"),
        ("买家上传图片 → 403", "POST", "/uploads"),
    ]:
        r = c.request(method, API + path, headers=bh, json={"name": "x"} if method == "POST" else None)
        check(name, r.status_code == 403, f"got {r.status_code}")

    # ---------- 4. 管理员（注册后提升） ----------
    admin_u = f"e2e_admin_{run_id}"
    admin_phone = f"139{str(int(time.time()))[-8:]}"
    c.post(API + "/auth/register", json={"username": admin_u, "password": pwd})
    admin_id = sql(f"SELECT id FROM users WHERE username='{admin_u}'")
    c.post(API + "/auth/verification/send", json={
        "channel": "phone", "target": admin_phone, "username": admin_u, "password": pwd})
    c.post(API + "/auth/verification/confirm", json={
        "channel": "phone", "target": admin_phone, "code": last_code(admin_id),
        "username": admin_u, "password": pwd})
    sql(f"UPDATE users SET is_admin=1 WHERE username='{admin_u}'")

    r = c.post(API + "/auth/login", json={"username": admin_u, "password": pwd})
    check("管理员登录成功", r.status_code == 200, r.text[:200])
    ah = {"Authorization": f"Bearer {r.json().get('access_token', '')}"}
    check("管理员 /auth/me 显示 is_admin", c.get(API + "/auth/me", headers=ah).json().get("is_admin") is True)

    # ---------- 5. 统计看板 ----------
    r = c.get(API + "/stats", headers=ah)
    body = r.json() if r.status_code == 200 else {}
    check("管理员可读统计概览", r.status_code == 200, r.text[:200])
    check("统计含订单/商品/用户/近期四块",
          {"orders", "products", "users", "recent"} <= set(body), str(list(body))[:200])
    check("订单 GMV 为数值", isinstance(body.get("orders", {}).get("paid_amount"), (int, float)),
          str(body.get("orders", {}).get("paid_amount")))
    r = c.get(API + "/stats/trend?days=7", headers=ah)
    check("管理员可读按天趋势", r.status_code == 200 and "items" in r.json(), r.text[:200])

    # ---------- 6. 下单主链路 ----------
    r = c.get(API + "/products?status=on_sale", headers=bh)
    items = r.json().get("items", [])
    check("商品列表返回在售商品", r.status_code == 200 and items, r.text[:200])
    sku_id = items[0]["skus"][0]["id"]
    stock_before = items[0]["skus"][0]["stock"]

    c.post(API + "/cart", headers=bh, json={"sku_id": sku_id, "quantity": 2})
    r = c.get(API + "/cart", headers=bh)
    cart = r.json()
    # 购物车行项里 sku 是名称快照，sku_id 才是 id，别按嵌套对象去取
    cart_list = cart.get("items", []) if isinstance(cart, dict) else cart
    cart_ids = [i.get("sku_id") for i in cart_list if isinstance(i, dict)]
    check("加购后购物车有该商品", sku_id in cart_ids, str(cart)[:200])

    r = c.post(API + "/cart/checkout", headers=bh, json={})
    check("结算生成订单", r.status_code == 201, r.text[:200])
    order = r.json()
    # 结算返回的是 order_id（不是 id），别按 orders 的序列化形状去取
    oid = order["order_id"]

    r = c.get(f"{API}/orders/{oid}", headers=bh)
    check("买家可查看自己的订单", r.status_code == 200, r.text[:200])

    # 未发货不能确认收货（引擎拦）
    r = c.post(f"{API}/orders/{oid}/actions/confirm", headers=bh, json={})
    check("未发货确认收货 → 400（引擎拦住）", r.status_code == 400, r.text[:200])

    # 推进：管理员支付、发货
    r = c.post(f"{API}/orders/{oid}/actions/pay", headers=ah, json={})
    check("管理员推进支付成功", r.status_code == 200, r.text[:200])
    r = c.post(f"{API}/orders/{oid}/actions/ship", headers=ah, json={})
    check("管理员推进发货成功", r.status_code == 200, r.text[:200])

    r = c.post(f"{API}/orders/{oid}/actions/confirm", headers=bh, json={})
    check("买家确认收货成功（买家自助动作生效）", r.status_code == 200, r.text[:200])

    # ---------- 7. 越权与隔离 ----------
    r = c.post(f"{API}/orders/{oid}/actions/ship", headers=bh, json={})
    check("买家触发管理员事件 → 403", r.status_code == 403, r.text[:200])

    other_oid = sql(f"SELECT id FROM orders WHERE user_id != {buyer_id} LIMIT 1")
    r = c.get(f"{API}/orders/{other_oid}", headers=bh)
    check("买家访问他人订单 → 404（不泄露存在性）", r.status_code == 404, r.text[:200])

    # ---------- 8. 取消与库存归还 ----------
    r = c.post(API + "/orders", headers=bh,
               json={"items": [{"sku_id": sku_id, "quantity": 1}]})
    check("买家直接下单成功", r.status_code == 201, r.text[:200])
    oid2 = r.json()["id"]
    r = c.post(f"{API}/orders/{oid2}/actions/cancel", headers=bh, json={})
    check("买家自助取消订单成功", r.status_code == 200, r.text[:200])
    stock_after = sql(f"SELECT stock FROM skus WHERE id={sku_id}")
    check("取消后库存已归还", int(stock_after) == int(stock_before) - 2,
          f"before={stock_before} after={stock_after}（应只扣掉已完成的 2 件）")

    # ---------- 9. 上传与静态访问 ----------
    png = (b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
           + (1).to_bytes(8, "big") + bytes([8, 6, 0, 0, 0]) + b"\x00\x00\x00\x00"
           + (0).to_bytes(4, "big") + b"IEND" + b"\xaeB\x60\x82")
    r = c.post(API + "/uploads", headers=ah,
               files={"file": ("a.png", png, "image/png")})
    check("管理员上传图片成功", r.status_code == 201, r.text[:200])
    url = r.json().get("url", "")
    r = anon.get(HOST + url)
    check("游客（未登录）可读取上传的图片", r.status_code == 200, f"{r.status_code}")
    check("图片响应带 nosniff 头",
          r.headers.get("x-content-type-options") == "nosniff", str(dict(r.headers))[:200])

    # ---------- 10. SKU 改价/调库存 ----------
    r = c.patch(f"{API}/products/{items[0]['id']}/skus/{sku_id}", headers=ah,
                json={"stock_delta": 5})
    check("管理员用 stock_delta 补货成功", r.status_code == 200, r.text[:200])

    # ---------- 11. 刷新令牌轮转与重放 ----------
    # 走 API 客户端路径（Bearer 头 + 响应体里的 refresh_token），不依赖 Cookie：
    # 生产模式 COOKIE_SECURE=true，非 HTTPS 下 Cookie 行为不可靠，测不出真实结论。
    r = c.post(API + "/auth/login", json={"username": buyer, "password": pwd})
    rt1 = r.json().get("refresh_token")
    check("登录返回刷新令牌", bool(rt1), r.text[:160] if not rt1 else "")

    def do_refresh(tok):
        # 每次用独立客户端，确保不复用任何 Cookie
        return httpx.Client(timeout=30).post(
            API + "/auth/refresh", headers={"Authorization": f"Bearer {tok}"})

    r1 = do_refresh(rt1)
    rt2 = r1.json().get("refresh_token") if r1.status_code == 200 else None
    check("刷新成功且轮转出新刷新令牌",
          r1.status_code == 200 and rt2 and rt2 != rt1, f"status={r1.status_code}")

    r2 = do_refresh(rt2) if rt2 else None
    rt3 = r2.json().get("refresh_token") if (r2 and r2.status_code == 200) else None
    check("轮转后的新令牌可继续使用",
          bool(r2) and r2.status_code == 200 and rt3 and rt3 != rt2,
          f"status={r2.status_code if r2 else 'skip'}")

    r3 = do_refresh(rt1)
    check("重放最初那条刷新令牌 → 401（判定为泄露）", r3.status_code == 401, f"got {r3.status_code}")

    if rt3:
        # 关键：撤销的是**整条 family**，而不只是被重放的那一条，
        # 所以连最新签发的令牌也应一并失效
        r4 = do_refresh(rt3)
        check("整族撤销后最新令牌也失效 → 401", r4.status_code == 401, f"got {r4.status_code}")

    r5 = do_refresh(buyer_token)
    check("拿访问令牌去刷新 → 401（令牌类型隔离）", r5.status_code == 401, f"got {r5.status_code}")

    # ---------- 12. 登出 ----------
    r = c.post(API + "/auth/logout", headers=bh)
    check("登出返回 200", r.status_code == 200, r.text[:200])

    # ---------- 汇总 ----------
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'=' * 60}")
    print(f"通过 {passed}/{len(results)}")
    for name, ok, detail in results:
        if not ok:
            print(f"  ✗ {name}  {detail}")
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
