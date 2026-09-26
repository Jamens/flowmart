"""统计看板（只读聚合）。

设计原则：
1. **只做聚合查询，不把明细行读进内存**。订单/商品行数会随时间增长，
   在 Python 里 sum() 迟早会把接口拖垮，也白白浪费 DB 往返。
2. **口径写清楚**：GMV 取「已支付（paid_at 非空）订单的 pay_amount 之和」，
   不是所有订单金额之和——未支付的订单不该计入销售额。
3. **权限 require_admin**：销售额、用户数、库存结构都是经营数据，
   不该对普通买家开放（买家在商城页只需要自己的订单）。

低库存阈值做成参数：不同品类「缺货」的标准不一样（手机 3 台算紧张、
数据线 30 条可能不算），写死一个数字没有意义。
"""
from datetime import datetime, time, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import require_admin
from app.models.ecommerce import Order, Product, Sku, User

router = APIRouter(prefix="/stats", tags=["统计"])

DEFAULT_LOW_STOCK = 10


def _money(value) -> float:
    """金额统一转 float：Numeric 直接进 JSON 响应会变成字符串，前端不好算。"""
    return float(value or 0)


@router.get("", summary="统计概览（仅管理员）")
def stats_overview(
    low_stock_threshold: int = Query(DEFAULT_LOW_STOCK, ge=0),
    recent_days: int = Query(7, ge=1, le=90),
    current_user=Depends(require_admin),
    db: Session = Depends(get_db),
):
    orders_total = db.execute(select(func.count()).select_from(Order)).scalar() or 0
    by_status = {
        status: count
        for status, count in db.execute(
            select(Order.status, func.count()).group_by(Order.status)
        ).all()
    }
    # GMV 口径：已支付订单的应付金额之和（未支付的不计入销售额）
    gmv = db.execute(
        select(func.coalesce(func.sum(Order.pay_amount), 0)).where(Order.paid_at.is_not(None))
    ).scalar()

    products_total = db.execute(select(func.count()).select_from(Product)).scalar() or 0
    on_sale = db.execute(
        select(func.count()).select_from(Product).where(Product.status == "on_sale")
    ).scalar() or 0
    sku_total = db.execute(select(func.count()).select_from(Sku)).scalar() or 0
    stock_total = db.execute(select(func.coalesce(func.sum(Sku.stock), 0))).scalar() or 0
    low_stock = db.execute(
        select(func.count()).select_from(Sku).where(Sku.stock <= low_stock_threshold)
    ).scalar() or 0

    users_total = db.execute(
        select(func.count()).select_from(User).where(User.is_active.is_(True))
    ).scalar() or 0

    since = datetime.now() - timedelta(days=recent_days)
    recent_orders = db.execute(
        select(func.count()).select_from(Order).where(Order.created_at >= since)
    ).scalar() or 0
    recent_amount = db.execute(
        select(func.coalesce(func.sum(Order.pay_amount), 0)).where(
            Order.created_at >= since, Order.paid_at.is_not(None)
        )
    ).scalar()

    return {
        "orders": {
            "total": orders_total,
            "by_status": by_status,
            # 已支付订单的应付金额合计（GMV），未支付订单不计入
            "paid_amount": _money(gmv),
        },
        "products": {
            "total": products_total,
            "on_sale": on_sale,
            "off_shelf": products_total - on_sale,
            "sku_total": sku_total,
            "stock_total": int(stock_total),
            "low_stock": low_stock,
            "low_stock_threshold": low_stock_threshold,
        },
        "users": {"active_total": users_total},
        "recent": {
            "days": recent_days,
            "orders": recent_orders,
            "paid_amount": _money(recent_amount),
        },
    }


@router.get("/trend", summary="按天统计订单与销售额（仅管理员；看板图用）")
def stats_trend(
    days: int = Query(7, ge=1, le=90),
    current_user=Depends(require_admin),
    db: Session = Depends(get_db),
):
    since = datetime.combine(datetime.now().date() - timedelta(days=days), time.min)
    rows = db.execute(
        select(
            func.date(Order.created_at).label("day"),
            func.count(),
            func.coalesce(func.sum(Order.pay_amount), 0),
        )
        .where(Order.created_at >= since)
        .group_by(func.date(Order.created_at))
        .order_by(func.date(Order.created_at))
    ).all()

    return {
        "days": days,
        "items": [
            {"date": str(day), "orders": count, "paid_amount": _money(amount)}
            for day, count, amount in rows
        ],
    }
