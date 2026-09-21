"""商品分类 API。

注意：products.category_id 是普通 Integer 而非外键，
所以删除分类不会触发任何数据库级约束 —— 必须在应用层校验是否有商品引用，
否则删掉分类后，商品会指向一个不存在的分类（静默产生脏数据）。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.ecommerce import Category, Product

router = APIRouter(prefix="/categories", tags=["分类"])


class CategoryIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    parent_id: int = 0
    sort: int = 0


class CategoryUpdateIn(BaseModel):
    name: str | None = None
    parent_id: int | None = None
    sort: int | None = None


def _get_category(db: Session, category_id: int) -> Category:
    c = db.get(Category, category_id)
    if c is None:
        raise HTTPException(status_code=404, detail="分类不存在")
    return c


def _count_products(db: Session, category_id: int) -> int:
    return (
        db.execute(
            select(func.count()).select_from(Product).where(Product.category_id == category_id)
        ).scalar()
        or 0
    )


@router.get("", summary="分类列表（带商品数量）")
def list_categories(parent_id: int | None = None, db: Session = Depends(get_db)):
    stmt = select(Category).order_by(Category.sort, Category.id)
    if parent_id is not None:
        stmt = stmt.where(Category.parent_id == parent_id)
    rows = db.execute(stmt).scalars().all()
    return [
        {
            "id": c.id,
            "name": c.name,
            "parent_id": c.parent_id,
            "sort": c.sort,
            "product_count": _count_products(db, c.id),
        }
        for c in rows
    ]


@router.get("/tree", summary="分类树")
def category_tree(db: Session = Depends(get_db)):
    """返回两级结构：顶层分类 + 其子分类。"""
    rows = db.execute(select(Category).order_by(Category.sort, Category.id)).scalars().all()
    nodes = [
        {
            "id": c.id,
            "name": c.name,
            "parent_id": c.parent_id,
            "sort": c.sort,
            "product_count": _count_products(db, c.id),
            "children": [],
        }
        for c in rows
    ]
    by_id = {n["id"]: n for n in nodes}
    roots = []
    for n in nodes:
        parent = by_id.get(n["parent_id"])
        if parent and n["parent_id"] != 0:
            parent["children"].append(n)
        else:
            roots.append(n)
    return roots


@router.post("", status_code=201, summary="创建分类")
def create_category(payload: CategoryIn, db: Session = Depends(get_db)):
    if payload.parent_id:
        _get_category(db, payload.parent_id)  # 父分类必须存在
    c = Category(name=payload.name, parent_id=payload.parent_id, sort=payload.sort)
    db.add(c)
    db.commit()
    db.refresh(c)
    return {"id": c.id, "name": c.name}


@router.patch("/{category_id}", summary="修改分类")
def update_category(
    category_id: int, payload: CategoryUpdateIn, db: Session = Depends(get_db)
):
    c = _get_category(db, category_id)
    if payload.parent_id is not None and payload.parent_id:
        # 不能把自己设成自己的父级，否则树会成环
        if payload.parent_id == category_id:
            raise HTTPException(status_code=400, detail="分类的父级不能是自己")
        _get_category(db, payload.parent_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(c, field, value)
    db.commit()
    return {"id": c.id, "name": c.name}


@router.delete("/{category_id}", summary="删除分类")
def delete_category(category_id: int, db: Session = Depends(get_db)):
    c = _get_category(db, category_id)

    children = (
        db.execute(select(func.count()).select_from(Category).where(Category.parent_id == c.id))
        .scalar()
        or 0
    )
    if children:
        raise HTTPException(status_code=400, detail=f"该分类下还有 {children} 个子分类")

    # category_id 不是外键，删库不会有任何数据库层提示，必须手动挡住
    used = _count_products(db, c.id)
    if used:
        raise HTTPException(status_code=400, detail=f"该分类下还有 {used} 个商品，不能删除")

    db.delete(c)
    db.commit()
    return {"id": category_id, "removed": True}
