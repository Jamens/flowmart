"""商品管理写操作：编辑商品 / 调整 SKU 价格与库存 / 删除商品。

背景：此前商品只有「列表 / 详情 / 创建 / 上下架」，创建后**改不了名称描述、删不掉**，
SKU 的改价与调库存更是完全没有入口。本文件锁定这三个新端点，重点覆盖：
  1. 改价 / 调库存 / 删除必须管理员（否则任意买家都能把价格改成 0.01 或给自己刷库存）；
  2. 删除必须先在应用层挡住引用（购物车有外键、订单明细是已售凭证），并提示改用下架；
  3. SKU 归属要带 product_id 一起校验，防止改个 sku_id 就改到别家商品的价。
"""
from decimal import Decimal

import pytest

from sqlalchemy import select

from app.core.security import hash_password
from app.models.ecommerce import CartItem, Category, Product, Sku, User


@pytest.fixture
def product(db):
    """一个带单个 SKU 的商品，供编辑 / 调价 / 删除用例使用。"""
    p = Product(name="待编辑商品", description="原描述", cover="", status="on_sale")
    db.add(p)
    db.flush()
    s = Sku(product_id=p.id, sku_code="EDIT-001", spec="默认",
            price=Decimal("100.00"), stock=10)
    db.add(s)
    db.commit()
    db.refresh(p)
    return p


def _make_buyer(db, username="buyer_p") -> User:
    u = User(username=username, nickname=username, phone="13800000456",
             password_hash=hash_password("123456"), is_admin=False, email_verified=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_admin_can_update_product(client, db, product):
    """管理员可编辑商品名称与描述。"""
    r = client.patch(
        f"/api/v1/products/{product.id}",
        json={"name": "新名字", "description": "新描述"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "新名字"
    assert r.json()["description"] == "新描述"


def test_update_does_not_wipe_omitted_fields(client, db, product):
    """局部更新：没传的字段必须保持原值，不能被清空。"""
    r = client.patch(f"/api/v1/products/{product.id}", json={"cover": "http://img/a.png"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["cover"] == "http://img/a.png"
    assert data["name"] == "待编辑商品", "未传的 name 不应被清空"
    assert data["description"] == "原描述", "未传的 description 不应被清空"


def test_update_missing_product_returns_404(client):
    assert client.patch("/api/v1/products/99999", json={"name": "x"}).status_code == 404


def test_buyer_cannot_update_product(buyer_client, product):
    """编辑商品是运营操作，买家一律 403。"""
    r = buyer_client.patch(f"/api/v1/products/{product.id}", json={"name": "被篡改"})
    assert r.status_code == 403


def test_admin_can_adjust_price(client, db, product):
    """管理员可调价。"""
    sku = product.skus[0]
    r = client.patch(
        f"/api/v1/products/{product.id}/skus/{sku.id}", json={"price": 88.5}
    )
    assert r.status_code == 200, r.text
    assert r.json()["price"] == 88.5
    db.refresh(sku)
    assert float(sku.price) == 88.5


def test_admin_can_adjust_stock(client, db, product):
    """管理员可调库存（补货场景）。"""
    sku = product.skus[0]
    r = client.patch(f"/api/v1/products/{product.id}/skus/{sku.id}", json={"stock": 42})
    assert r.status_code == 200, r.text
    assert r.json()["stock"] == 42
    db.refresh(sku)
    assert sku.stock == 42


def test_buyer_cannot_adjust_price_or_stock(buyer_client, product):
    """改价 / 调库存是最危险的写操作，买家必须 403——否则可把价格改成 0.01 或刷库存。"""
    sku = product.skus[0]
    base = f"/api/v1/products/{product.id}/skus/{sku.id}"
    assert buyer_client.patch(base, json={"price": 0.01}).status_code == 403
    assert buyer_client.patch(base, json={"stock": 99999}).status_code == 403


def test_sku_of_other_product_returns_404(client, db, product):
    """SKU 归属要带 product_id 一起校验：别家商品的 SKU 挂过来应 404，而不是被改价。"""
    other = Product(name="别的商品", status="on_sale")
    db.add(other)
    db.flush()
    other_sku = Sku(product_id=other.id, sku_code="OTHER-001",
                    price=Decimal("9.90"), stock=1)
    db.add(other_sku)
    db.commit()

    r = client.patch(
        f"/api/v1/products/{product.id}/skus/{other_sku.id}", json={"price": 1}
    )
    assert r.status_code == 404
    # 确认没有被误改
    db.refresh(other_sku)
    assert float(other_sku.price) == 9.9


def test_admin_can_delete_unreferenced_product(client, db, product):
    """无引用的商品可删除，且 SKU 被级联清理。"""
    sku_id = product.skus[0].id
    r = client.delete(f"/api/v1/products/{product.id}")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True
    assert db.get(Product, product.id) is None
    assert db.get(Sku, sku_id) is None, "SKU 应随商品级联删除"


def test_delete_refused_when_sku_in_cart(client, db, product):
    """购物车行项对 skus.id 有外键：真删会被 DB 拒绝或留悬空引用，必须在应用层挡住。"""
    buyer = _make_buyer(db)
    sku = product.skus[0]
    db.add(CartItem(user_id=buyer.id, sku_id=sku.id, quantity=1))
    db.commit()

    r = client.delete(f"/api/v1/products/{product.id}")
    assert r.status_code == 400
    assert "购物车" in r.json()["detail"]
    assert db.get(Product, product.id) is not None, "被拒绝时商品必须还在"


def test_delete_refused_when_already_sold(client, db, product):
    """已产生历史订单明细的商品不能硬删（会让订单明细悬空），应提示改用下架。"""
    buyer = _make_buyer(db)
    sku = product.skus[0]

    client.as_user(buyer)
    r = client.post("/api/v1/orders", json={"items": [{"sku_id": sku.id, "quantity": 1}]})
    assert r.status_code == 201, r.text

    client.as_user(client.user)  # 回到管理员
    r = client.delete(f"/api/v1/products/{product.id}")
    assert r.status_code == 400
    assert "历史订单" in r.json()["detail"]


def test_buyer_cannot_delete_product(buyer_client, product):
    assert buyer_client.delete(f"/api/v1/products/{product.id}").status_code == 403


# ---------------- 创建与上下架也必须管理员 ----------------
# 否则新增的改价闸门形同虚设：买家绕开改价接口，直接 POST 一个
# price=0.01 / stock=99999 的自有商品，效果完全一样。


def test_buyer_cannot_create_product(buyer_client, db):
    """创建商品必须管理员，否则买家可自建 0.01 元商品 + 海量库存。"""
    r = buyer_client.post(
        "/api/v1/products",
        json={"name": "白嫖商品",
              "skus": [{"sku_code": "HACK-1", "price": 0.01, "stock": 99999}]},
    )
    assert r.status_code == 403
    assert db.execute(
        select(Sku).where(Sku.sku_code == "HACK-1")
    ).scalars().first() is None, "被拒绝时不应留下任何 SKU"


def test_buyer_cannot_toggle_shelf(buyer_client, db, product):
    """上下架必须管理员：否则一个买家就能把全站商品下架、让商城列表空掉。"""
    r = buyer_client.patch(
        f"/api/v1/products/{product.id}/shelf", params={"on_sale": "false"}
    )
    assert r.status_code == 403
    db.refresh(product)
    assert product.status == "on_sale", "被拒绝时上下架状态必须不变"


# ---------------- stock_delta：相对量补货 ----------------


def test_admin_can_replenish_with_delta(client, db, product):
    """stock_delta 是相对量补货，走原子 UPDATE。"""
    sku = product.skus[0]
    assert sku.stock == 10
    r = client.patch(
        f"/api/v1/products/{product.id}/skus/{sku.id}", json={"stock_delta": 5}
    )
    assert r.status_code == 200, r.text
    assert r.json()["stock"] == 15
    db.refresh(sku)
    assert sku.stock == 15


def test_delta_making_stock_negative_is_rejected(client, db, product):
    """调整后会变负数的 stock_delta 必须被拒，且库存不变。"""
    sku = product.skus[0]
    r = client.patch(
        f"/api/v1/products/{product.id}/skus/{sku.id}", json={"stock_delta": -999}
    )
    assert r.status_code == 400
    db.refresh(sku)
    assert sku.stock == 10, "被拒绝时库存必须保持不变"


def test_stock_and_delta_together_is_rejected(client, product):
    """绝对值与相对量语义冲突，同时传必须 400 而不是挑一个生效。"""
    sku = product.skus[0]
    r = client.patch(
        f"/api/v1/products/{product.id}/skus/{sku.id}",
        json={"stock": 1, "stock_delta": 1},
    )
    assert r.status_code == 400


# ---------------- 分类名语义 ----------------


def test_empty_category_name_clears_category(client, db, product):
    """传空串 = 取消归类，绝不能建出 Category(name="")。"""
    r = client.patch(f"/api/v1/products/{product.id}", json={"category_name": ""})
    assert r.status_code == 200, r.text
    assert db.execute(
        select(Category).where(Category.name == "")
    ).scalars().first() is None


def test_category_name_is_trimmed_before_lookup(client, db, product):
    """带空白的分类名应 strip 后命中已有分类，而不是静默新建同义分类
    （Category.name 无唯一约束，重复节点只能人工清理）。"""
    db.add(Category(name="手机"))
    db.commit()

    r = client.patch(f"/api/v1/products/{product.id}", json={"category_name": " 手机 "})
    assert r.status_code == 200, r.text
    cats = db.execute(select(Category).where(Category.name.like("%手机%"))).scalars().all()
    assert len(cats) == 1, "不应新建重复分类"
