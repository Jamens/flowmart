"""商品与库存 API。"""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.database import get_db
from app.core.pagination import apply_pagination, total_count
from app.core.security import get_current_user, require_admin
from app.models.ecommerce import CartItem, Category, OrderItem, Product, Sku, User

router = APIRouter(prefix="/products", tags=["商品"])


class SkuIn(BaseModel):
    sku_code: str = Field(..., min_length=1, max_length=64)
    spec: str = ""
    price: Decimal = Field(..., gt=0)
    stock: int = Field(0, ge=0)


class ProductIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str = ""
    cover: str = ""
    category_name: str = ""
    skus: list[SkuIn] = Field(default_factory=list)


class ProductUpdateIn(BaseModel):
    """商品编辑：字段全部可选，只更新显式传入的部分。

    刻意不做「全量覆盖」：前端只改一个字段时不该被迫回传整表，
    也更不容易漏传导致字段被清空。
    """

    # 长度与列宽对齐（Category.name 是 String(64)、Product.cover 是 String(255)）：
    # 不约束的话 MySQL 严格模式超长直接 500，而 SQLite 不校验长度、测试根本跑不出来。
    name: str | None = Field(None, min_length=1, max_length=128)
    description: str | None = None
    cover: str | None = Field(None, max_length=255)
    category_name: str | None = Field(None, max_length=64)


class SkuUpdateIn(BaseModel):
    """SKU 调整：价格与库存可独立调整。改价/调库存都是运营操作，必须管理员。

    stock 与 stock_delta 二选一，语义完全不同：
    - stock       = 绝对值「盘点修正」，会覆盖并发期间的扣减（见 update_sku 注释）
    - stock_delta = 相对量「补货/扣减」，走原子 UPDATE，不会丢更新
    """

    price: Decimal | None = Field(None, gt=0, decimal_places=2)
    stock: int | None = Field(None, ge=0)
    stock_delta: int | None = None
    spec: str | None = Field(None, max_length=255)


class SkuOut(BaseModel):
    id: int
    sku_code: str
    spec: str
    price: float
    stock: int
    status: str

    model_config = {"from_attributes": True}


class ProductOut(BaseModel):
    id: int
    name: str
    description: str
    cover: str
    status: str
    skus: list[SkuOut] = []

    model_config = {"from_attributes": True}


class ProductListOut(BaseModel):
    """列表接口信封响应：items 是 ProductOut 列表，total 是过滤后总数（与分页无关）。

    为什么需要它：list_products 现在返回 {items, total} 信封而非裸列表，
    若仍挂 response_model=list[ProductOut]，FastAPI 会把字典当列表校验、
    在生产环境直接抛 ResponseValidationError（500）。items 是 ORM 对象，
    靠 ProductOut.from_attributes 递归序列化。
    """

    items: list[ProductOut]
    total: int

    model_config = {"from_attributes": True}


@router.get("", response_model=ProductListOut, summary="商品列表（含 SKU）")
def list_products(
    keyword: str = "",
    status: str = "on_sale",
    # limit=0 表示不分页（返回全部）：SKU 下拉框要拿全量商品，不能被截断
    limit: int = Query(0, ge=0, le=500),
    offset: int = Query(0, ge=0),
    # 补鉴权：README 约定「除 /health 与 auth 外所有接口都必须携带身份凭证」，
    # 此前该接口（以及下面的上下架）漏了依赖，未登录也能调用
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(Product).options(selectinload(Product.skus))
    if keyword:
        stmt = stmt.where(Product.name.like(f"%{keyword}%"))
    if status:
        stmt = stmt.where(Product.status == status)
    total = total_count(db, stmt)
    products = (
        db.execute(apply_pagination(stmt.order_by(Product.id), limit, offset))
        .scalars()
        .unique()
        .all()
    )
    for p in products:
        for s in p.skus:
            s.price = float(s.price)
    return {"items": products, "total": total}


@router.get("/{product_id}", response_model=ProductOut, summary="商品详情")
def get_product(
    product_id: int,
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    product = db.execute(
        select(Product).options(selectinload(Product.skus)).where(Product.id == product_id)
    ).scalars().unique().first()
    if product is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    for s in product.skus:
        s.price = float(s.price)
    return product


@router.post("", response_model=ProductOut, status_code=201, summary="创建商品（含 SKU，仅管理员）")
def create_product(
    payload: ProductIn,
    # 创建也必须管理员，否则新增的「改价闸门」形同虚设：
    # 买家绕开改价接口，直接 POST 一个 price=0.01 / stock=99999 的自有商品，
    # 就能达到完全一样的效果，还能批量灌商品污染列表与搜索。
    current_user: User = Depends(require_admin), db: Session = Depends(get_db),
):
    # sku_code 唯一，重复要提前报错而不是等数据库抛完整性错误
    for sku in payload.skus:
        exist = db.execute(select(Sku).where(Sku.sku_code == sku.sku_code)).scalars().first()
        if exist:
            raise HTTPException(status_code=400, detail=f"SKU 编码 {sku.sku_code} 已存在")

    category_id = 0
    if payload.category_name:
        category = db.execute(
            select(Category).where(Category.name == payload.category_name)
        ).scalars().first()
        if category is None:
            category = Category(name=payload.category_name)
            db.add(category)
            db.flush()
        category_id = category.id

    product = Product(
        name=payload.name,
        description=payload.description,
        cover=payload.cover,
        category_id=category_id,
    )
    db.add(product)
    db.flush()
    for sku in payload.skus:
        db.add(
            Sku(
                product_id=product.id,
                sku_code=sku.sku_code,
                spec=sku.spec,
                price=sku.price,
                stock=sku.stock,
            )
        )
    db.commit()
    db.refresh(product)
    for s in product.skus:
        s.price = float(s.price)
    return product


@router.patch("/{product_id}/shelf", summary="上架/下架（仅管理员）")
def toggle_shelf(
    product_id: int,
    on_sale: bool = True,
    # 只「必须登录」是不够的：商品列表默认只筛在售，任意买家一个请求就能把
    # 全站商品下架、让商城列表空掉（单请求可用性破坏），还能把管理员因合规
    # 下架的商品重新上架。所以这里要的是管理员闸门，而不只是鉴权。
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    product.status = "on_sale" if on_sale else "off_shelf"
    db.commit()
    return {"id": product.id, "status": product.status}


# ---------------- 商品编辑 / SKU 调整 / 删除 ----------------
# 这三个都是运营写操作，统一 require_admin 闸门：
# 改价与调库存尤其不能放开——否则任意买家都能把价格改成 0.01 或给自己刷库存。


def _load_product(db: Session, product_id: int) -> Product:
    """按 id 取商品（含 SKU）；不存在则 404。"""
    product = db.execute(
        select(Product).options(selectinload(Product.skus)).where(Product.id == product_id)
    ).scalars().unique().first()
    if product is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    return product


def _to_out(product: Product) -> Product:
    """SKU 价格是 Decimal，必须先转 float 才能过 ProductOut 校验。"""
    for s in product.skus:
        s.price = float(s.price)
    return product


@router.patch("/{product_id}", response_model=ProductOut, summary="编辑商品（仅管理员）")
def update_product(
    product_id: int,
    payload: ProductUpdateIn,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    product = _load_product(db, product_id)
    if payload.name is not None:
        product.name = payload.name
    if payload.description is not None:
        product.description = payload.description
    if payload.cover is not None:
        product.cover = payload.cover
    if payload.category_name is not None:
        # 空串 = 取消归类，与创建接口「空串不挂分类」语义保持一致；
        # 绝不能拿空串去建一个 Category(name="") 出来。
        # 先 strip 再查：否则 "手机 "（带尾空格）匹配不到已有分类、会静默新建同义分类
        # ——Category.name 没有唯一约束，这种垃圾节点只能靠人工清理。
        name = payload.category_name.strip()
        if not name:
            product.category_id = 0
        else:
            category = db.execute(
                select(Category).where(Category.name == name)
            ).scalars().first()
            if category is None:
                category = Category(name=name)
                db.add(category)
                db.flush()
            product.category_id = category.id
    db.commit()
    db.refresh(product)
    return _to_out(product)


@router.patch("/{product_id}/skus/{sku_id}", response_model=SkuOut, summary="调整 SKU 价格 / 库存（仅管理员）")
def update_sku(
    product_id: int,
    sku_id: int,
    payload: SkuUpdateIn,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    # 同时约束 product_id 与 sku_id：只认「这个商品下的这个 SKU」，
    # 否则改个 sku_id 就能越权改到别家商品的价
    sku = db.execute(
        select(Sku).where(Sku.id == sku_id, Sku.product_id == product_id)
    ).scalars().first()
    if sku is None:
        raise HTTPException(status_code=404, detail="SKU 不存在")
    if payload.stock is not None and payload.stock_delta is not None:
        raise HTTPException(
            status_code=400,
            detail="stock 与 stock_delta 不能同时传：前者是盘点修正（绝对值），后者是补货/扣减（相对量）",
        )
    if payload.price is not None:
        sku.price = payload.price
    if payload.spec is not None:
        sku.spec = payload.spec
    if payload.stock_delta is not None:
        # 相对量走原子 UPDATE：「读出来加减再赋值」会与并发下单的扣减互相覆盖。
        # 本仓在库存上已有纪律——下单扣减是 UPDATE ... WHERE stock >= qty、
        # 归还是 UPDATE ... SET stock = stock + qty，都是原子的，这里必须同层保证，
        # 否则补货期间卖掉的那几件会凭空回到库存里（真实超卖）。
        res = db.execute(
            update(Sku)
            .where(Sku.id == sku.id, Sku.stock + payload.stock_delta >= 0)
            .values(stock=Sku.stock + payload.stock_delta)
        )
        if res.rowcount != 1:
            raise HTTPException(status_code=400, detail="库存调整后会变成负数，已拒绝")
    elif payload.stock is not None:
        # 绝对值写入（盘点修正）：语义就是「以我为准」，会覆盖并发期间的扣减，
        # 仅用于盘点纠偏；补货请用 stock_delta。
        sku.stock = payload.stock
    db.commit()
    db.refresh(sku)
    sku.price = float(sku.price)
    return sku


@router.delete("/{product_id}", summary="删除商品（仅管理员；已被引用则拒绝）")
def delete_product(
    product_id: int,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    product = _load_product(db, product_id)
    sku_ids = [s.id for s in product.skus]
    if sku_ids:
        # 购物车行项对 skus.id 有外键：真删会被 DB 拒绝（MySQL）或留下悬空引用（SQLite），
        # 必须在应用层先拦住。订单行项虽是快照、无外键，但删掉会让历史订单的 sku_id 悬空。
        # 与分类删除同一套路：有引用就拒绝，让调用方改用「下架」而不是硬删。
        in_cart = db.execute(
            select(func.count()).select_from(CartItem).where(CartItem.sku_id.in_(sku_ids))
        ).scalar()
        if in_cart:
            raise HTTPException(
                status_code=400,
                detail=f"该商品有 {in_cart} 个 SKU 仍在用户购物车中，无法删除（建议改为下架）",
            )
        sold = db.execute(
            select(func.count()).select_from(OrderItem).where(OrderItem.sku_id.in_(sku_ids))
        ).scalar()
        if sold:
            raise HTTPException(
                status_code=400,
                detail=f"该商品已产生 {sold} 条历史订单明细，删除会让订单明细悬空（建议改为下架）",
            )
    # SKU 由 Product.skus 关系的 delete-orphan 级联删除，无需手工清理。
    # 「先校验引用、再删除」之间仍有 TOCTOU 窗口：窗口内有人加购，MySQL 下会撞
    # cart_items.sku_id 外键抛 IntegrityError。不捕获就会变成 500，与本接口承诺的
    # 「400 + 建议改用下架」不符，所以在这里兜住。
    try:
        db.delete(product)
        db.commit()
    except IntegrityError:
        db.rollback()  # IntegrityError 之后会话已处于不可用状态，必须显式回滚
        raise HTTPException(
            status_code=400,
            detail="删除期间出现了新的购物车引用（并发加购），请重试或改为下架",
        )
    return {"id": product_id, "deleted": True}
