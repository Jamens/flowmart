"""API 集成测试。

为什么需要这一层：引擎单测覆盖不到「接口接线是否正确」。
例如 admin_db 的 list_tables 曾把 get_pk_constraint 返回的字符串列表当成
字典列表去取 c["name"]，这个 bug 在 SQLite / MySQL 下都会 500，
但因为没有任何接口测试，它一直潜伏到手工验证时才暴露。

策略：用 FastAPI TestClient + 覆写 get_db 依赖，让接口跑在临时 SQLite 上，
既真实走 HTTP 栈与依赖注入，又不依赖外部数据库。
"""
import pytest

from app.models.ecommerce import Product, Sku


@pytest.fixture
def sku(db):
    """一个可用于下单的 SKU。"""
    product = Product(name="测试商品", status="on_sale")
    db.add(product)
    db.flush()
    s = Sku(product_id=product.id, sku_code="T-001", spec="默认", price=100, stock=10)
    db.add(s)
    db.commit()
    return s


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_create_order_starts_workflow(client, sku):
    """下单后应自动启动流程并停在待付款。订单归属当前登录用户。"""
    r = client.post(
        "/api/v1/orders",
        json={"items": [{"sku_id": sku.id, "quantity": 2}]},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["status"] == "pending_payment"
    assert data["pay_amount"] == 200  # 100 × 2
    assert "pay" in [e["event"] for e in data["available_events"]]
    # 订单归属当前用户（不再从请求体读 user_id）
    assert data["user_id"] == client.user.id
    # 库存应同步扣减
    assert client.get(f"/api/v1/products/{sku.product_id}").json()["skus"][0]["stock"] == 8


def test_pay_advances_to_paid(client, sku):
    r = client.post(
        "/api/v1/orders",
        json={"items": [{"sku_id": sku.id, "quantity": 1}]},
    )
    order = r.json()
    r2 = client.post(f"/api/v1/orders/{order['id']}/actions/pay")
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "paid"
    # 时间线应记录 start -> submit -> pay 三步
    assert [t["event"] for t in r2.json()["timeline"]] == ["start", "submit", "pay"]


def test_illegal_action_returns_400(client, sku):
    """待付款状态下发货必须被拒绝，而不是静默成功。"""
    order = client.post(
        "/api/v1/orders",
        json={"items": [{"sku_id": sku.id, "quantity": 1}]},
    ).json()
    r = client.post(f"/api/v1/orders/{order['id']}/actions/ship")
    assert r.status_code == 400
    assert "不可执行" in r.json()["detail"]


def test_insufficient_stock_returns_400(client, sku):
    r = client.post(
        "/api/v1/orders",
        json={"items": [{"sku_id": sku.id, "quantity": 999}]},
    )
    assert r.status_code == 400
    assert "库存不足" in r.json()["detail"]


def test_publish_rejects_invalid_definition(client):
    """缺少 end 节点的流程不允许发布，把问题挡在运行之前。"""
    r = client.post(
        "/api/v1/workflows/definitions",
        json={
            "code": "bad_flow",
            "name": "缺终点流程",
            "nodes": [{"key": "start", "name": "开始", "node_type": "start"}],
            "transitions": [],
        },
    )
    assert r.status_code == 201
    def_id = r.json()["id"]
    r2 = client.post(f"/api/v1/workflows/definitions/{def_id}/publish")
    assert r2.status_code == 400
    assert "end" in str(r2.json()["detail"])


def test_admin_db_tables_returns_pk_as_strings(client, sku):
    """回归测试：pk 字段必须是列名字符串列表。

    曾误把 constrained_columns（如 ['id']）当成字典列表取值导致 500。
    """
    r = client.get("/api/v1/admin/db/tables")
    assert r.status_code == 200, r.text
    tables = {t["name"]: t for t in r.json()["tables"]}
    assert "skus" in tables
    assert tables["skus"]["pk"] == ["id"]


def test_admin_db_rejects_write_sql(client):
    """只读接口必须挡住写操作。"""
    r = client.post("/api/v1/admin/db/query", json={"sql": "DELETE FROM orders"})
    assert r.status_code == 400


def test_admin_db_rejects_unknown_table(client):
    r = client.get("/api/v1/admin/db/tables/not_exist")
    assert r.status_code == 404


def test_list_products_requires_auth(raw_client):
    """商品列表也必须登录：README 约定除 /health 与 auth 外所有接口都要凭证。

    该接口此前漏了 get_current_user 依赖，与文档契约不符。
    """
    r = raw_client.get("/api/v1/products")
    assert r.status_code == 401


def test_toggle_shelf_requires_auth(raw_client, sku):
    """上下架是运营操作，未登录必须 401。

    否则任何人无需登录就能把商品下架，直接影响业务可用性。
    """
    r = raw_client.patch(f"/api/v1/products/{sku.product_id}/shelf?on_sale=false")
    assert r.status_code == 401


def test_delete_category_requires_auth(raw_client):
    """删分类会改变商品归类，未登录必须 401（此前该接口漏了鉴权）。"""
    r = raw_client.delete("/api/v1/categories/1")
    assert r.status_code == 401


def test_list_categories_requires_auth(raw_client):
    """分类列表/树同样需要登录（与 README「除 health/auth 外都要凭证」一致）。"""
    assert raw_client.get("/api/v1/categories").status_code == 401
    assert raw_client.get("/api/v1/categories/tree").status_code == 401


def test_workflow_definition_endpoints_require_auth(raw_client):
    """流程定义直接驱动订单状态机，创建/修改/归档绝不能匿名调用。

    这 5 个端点此前完全没有鉴权依赖。
    """
    payload = {"code": "x", "name": "x", "nodes": [], "transitions": []}
    assert raw_client.get("/api/v1/workflows/definitions/1").status_code == 401
    assert raw_client.get("/api/v1/workflows/definitions/code/order_flow").status_code == 401
    assert raw_client.post("/api/v1/workflows/definitions", json=payload).status_code == 401
    assert raw_client.put("/api/v1/workflows/definitions/1", json=payload).status_code == 401
    assert raw_client.delete("/api/v1/workflows/definitions/1").status_code == 401


def test_admin_db_data_endpoints_require_admin(buyer_client):
    """表数据预览 / 只读 SQL 能拖走全库（含 users.password_hash），必须管理员。"""
    assert buyer_client.get("/api/v1/admin/db/tables/users").status_code == 403
    r = buyer_client.post("/api/v1/admin/db/query", json={"sql": "select * from users"})
    assert r.status_code == 403


def test_admin_db_data_accessible_by_admin(client):
    """反向断言：管理员仍可正常读取，避免把闸门收得过头。"""
    assert client.get("/api/v1/admin/db/tables/users").status_code == 200
    r = client.post("/api/v1/admin/db/query", json={"sql": "select id from users"})
    assert r.status_code == 200, r.text


def test_update_category_ignores_explicit_null_sort(client):
    """前端清空数字输入框会显式传 sort=null。

    若照单全收会把 None 写进 NOT NULL 的 sort 列而 500，正确做法是忽略该字段。
    """
    r = client.post("/api/v1/categories", json={"name": "空值排序分类", "sort": 5})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]

    r2 = client.patch(f"/api/v1/categories/{cid}", json={"sort": None})
    assert r2.status_code == 200, r2.text

    row = next(c for c in client.get("/api/v1/categories").json() if c["id"] == cid)
    assert row["sort"] == 5, "显式 null 应被忽略，原值保留"
