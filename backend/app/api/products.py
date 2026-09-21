"""商品与库存 API。"""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.database import get_db
from app.core.pagination import apply_pagination, total_count
from app.core.security import get_current_user
from app.models.ecommerce import Category, Product, Sku, User

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
    limit: int = 0,
    offset: int = 0,
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


@router.post("", response_model=ProductOut, status_code=201, summary="创建商品（含 SKU）")
def create_product(
    payload: ProductIn,
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db),
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


@router.patch("/{product_id}/shelf", summary="上架/下架")
def toggle_shelf(
    product_id: int,
    on_sale: bool = True,
    # 上下架是运营操作：未登录即可调用的话，任何人都能把商品下架，必须鉴权
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    product.status = "on_sale" if on_sale else "off_shelf"
    db.commit()
    return {"id": product.id, "status": product.status}
