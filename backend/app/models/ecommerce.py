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
    UniqueConstraint,
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
    # 密码哈希，格式见 app/core/security.hash_password；留空表示尚未设置密码
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # 最后一次改密时间（UTC、naive）：用于「改密后失效旧令牌」——签发时间早于该值的令牌一律判失效。
    # 必须按 **UTC** 存：JWT 的 iat 是 Unix 时间戳（UTC 基准），若用本地时间存，
    # 与 iat 比较会整体偏移，导致令牌要么全失效、要么永不失效。
    # NULL = 从未改过密码，所有现存令牌保持有效（存量数据向后兼容，无需刷数据）。
    pwd_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 是否管理员：RBAC 角色闸门（require_admin）的依据。普通用户只能操作自己的资源，
    # 建账号 / 禁用用户 / 推进订单流转等管理操作仅管理员可执行。
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    # 邮箱与验证状态（邮箱/手机验证功能使用）。email 可空、未验证前为 ""；
    # *_verified 标记该渠道是否已完成验证，置位的同时会把 target 绑定到账号上。
    email: Mapped[str] = mapped_column(String(255), nullable=True, default="", server_default="")
    email_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    phone_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )


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
    """购物车项。

    (user_id, sku_id) 唯一：同一商品重复加入时累加数量而非新增一行。
    这条约束是数据层兜底 —— 「不新增行」不能只靠应用层保证，
    并发请求或绕过 API 的写入都可能插入重复行。
    """

    __tablename__ = "cart_items"
    __table_args__ = (UniqueConstraint("user_id", "sku_id", name="uq_cart_user_sku"),)

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
    # 长度必须 >= wf_nodes.key 的长度(64)，否则 MySQL strict 模式会因超长直接写入失败，
    # 而 SQLite 不校验长度会静默放行 —— 这类差异要提前消灭在数据模型层面
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
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


class VerificationCode(TimestampMixin, Base):
    """验证码：邮箱/手机验证用的一次性 OTP。

    channel=email|phone，target 为收件邮箱或手机号，code 为一次性口令。
    expires_at 之后失效；consumed_at 非 NULL 表示已被使用（作废旧码防重放）。
    """

    __tablename__ = "verification_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )
    # email | phone
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    # 收件邮箱或手机号
    target: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False, default="verify")
    # 过期时间；超过即失效
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # 使用时间；非 NULL 表示已消费
    consumed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    # 确认尝试次数：达到上限即锁定（置 consumed_at），防对单个码暴力枚举
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    user: Mapped["User"] = relationship("User")
