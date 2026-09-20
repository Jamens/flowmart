"""电商域数据模型。

设计取舍：
- 金额一律用 Numeric(12,2)，绝不用 float —— float 做累加会产生 0.1+0.2!=0.3 这类误差，钱的事不能忍。
- 订单行项冗余 sku_name/price：商品改价或下架后，历史订单必须保持下单时的快照，不能跟着变。
- 订单冗余 status：订单列表要按状态筛选排序，避免每次 join 流程实例表。
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class TimestampMixin:
    """创建/更新时间。用数据库函数而非 Python 时间，避免应用服务器时钟漂移。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    nickname: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    phone: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Address(TimestampMixin, Base):
    __tablename__ = "addresses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )
    receiver: Mapped[str] = mapped_column(String(64), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    province: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    city: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    district: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    detail: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Category(TimestampMixin, Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sort: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Product(TimestampMixin, Base):
    """商品 SPU。价格与库存下沉到 SKU 层。"""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    cover: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # on_sale=在售, off_shelf=已下架
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="on_sale")

    skus: Mapped[list["Sku"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )


class Sku(TimestampMixin, Base):
    """商品 SKU：真正承载价格与库存的一层。"""

    __tablename__ = "skus"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("products.id"), nullable=False, index=True
    )
    sku_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    spec: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="on_sale")

    product: Mapped["Product"] = relationship(back_populates="skus")


class CartItem(TimestampMixin, Base):
    __tablename__ = "cart_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )
    sku_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("skus.id"), nullable=False, index=True
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class Order(TimestampMixin, Base):
    """订单主表。

    status 与 current_node_key 由工作流引擎写入，业务代码不直接改状态——
    这是保证"所有流转都留痕、都合法"的关键约束。
    """

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )
    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    pay_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    # 冗余字段：订单列表筛选排序用，由引擎同步
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="", index=True)
    current_node_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    workflow_instance_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    # 收货地址快照：地址被改或删除后，订单仍要能正常发货
    address_snapshot: Mapped[str] = mapped_column(Text, nullable=False, default="")
    remark: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    paid_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    shipped_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderItem(TimestampMixin, Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("orders.id"), nullable=False, index=True
    )
    sku_id: Mapped[int] = mapped_column(Integer, nullable=False)
    sku_name: Mapped[str] = mapped_column(String(128), nullable=False)
    spec: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    order: Mapped["Order"] = relationship(back_populates="items")


class Payment(TimestampMixin, Base):
    """支付记录：模拟支付渠道，记录流水便于对账。"""

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("orders.id"), nullable=False, index=True
    )
    pay_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="mock")
    # pending=待支付, success=已支付, failed=失败, refunded=已退款
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    paid_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
