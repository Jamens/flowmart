"""商品与库存 API。"""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.database import get_db
from app.models.ecommerce import Category, Product, Sku

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


@router.get("", response_model=list[ProductOut], summary="商品列表（含 SKU）")
def list_products(
    keyword: str = "", status: str = "on_sale", db: Session = Depends(get_db)
):
    stmt = select(Product).options(selectinload(Product.skus))
    if keyword:
        stmt = stmt.where(Product.name.like(f"%{keyword}%"))
    if status:
        stmt = stmt.where(Product.status == status)
    products = db.execute(stmt.order_by(Product.id)).scalars().unique().all()
    for p in products:
        for s in p.skus:
            s.price = float(s.price)
    return products


@router.get("/{product_id}", response_model=ProductOut, summary="商品详情")
def get_product(product_id: int, db: Session = Depends(get_db)):
    product = db.execute(
        select(Product).options(selectinload(Product.skus)).where(Product.id == product_id)
    ).scalars().unique().first()
    if product is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    for s in product.skus:
        s.price = float(s.price)
    return product


@router.post("", response_model=ProductOut, status_code=201, summary="创建商品（含 SKU）")
def create_product(payload: ProductIn, db: Session = Depends(get_db)):
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
def toggle_shelf(product_id: int, on_sale: bool = True, db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    product.status = "on_sale" if on_sale else "off_shelf"
    db.commit()
    return {"id": product.id, "status": product.status}
