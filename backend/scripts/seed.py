"""灌入演示数据：流程定义 + 商品 + 覆盖各状态的订单。

设计要点：订单不是手工 INSERT 的，而是真实调用 OrderService 走一遍流程引擎。
这样 wf_instances / wf_transition_logs / 库存变动都是引擎真实产生的，
而不是对不上号的假数据 —— 否则演示时一查流转日志就露馅。

用法：
    python scripts/seed.py            # 幂等：已有数据则跳过
    python scripts/seed.py --reset    # 清空业务数据后重建
"""
import argparse
import sys
from decimal import Decimal
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import select  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models.ecommerce import (  # noqa: E402
    Address,
    Category,
    Order,
    OrderItem,
    Payment,
    Product,
    Sku,
    User,
)
from app.models.workflow import (  # noqa: E402
    WorkflowDefinition,
    WorkflowNode,
    WorkflowTransition,
    WorkflowInstance,
    WorkflowTransitionLog,
)
from app.services.order_service import OrderService  # noqa: E402

# ---------------- 流程定义 ----------------
# 节点坐标供前端设计器画布直接渲染
NODES = [
    ("start", "开始", "start", 60, 220),
    ("pending_payment", "待付款", "task", 230, 220),
    ("paid", "待发货", "task", 400, 220),
    ("shipped", "已发货", "task", 570, 220),
    ("completed", "已完成", "end", 740, 220),
    ("closed", "已关闭", "end", 400, 60),
    ("refunding", "退款审核中", "task", 570, 60),
]

TRANSITIONS = [
    ("start", "pending_payment", "submit", "", 10, "用户提交订单"),
    ("pending_payment", "paid", "pay", "", 10, "支付成功"),
    ("pending_payment", "closed", "cancel", "", 10, "取消订单"),
    ("paid", "shipped", "ship", "", 10, "商家发货"),
    # 同一事件 refund 按金额分流：小额自动关单，大额转人工审核
    ("paid", "closed", "refund", "amount < 1000", 10, "小额退款自动关闭"),
    ("paid", "refunding", "refund", "amount >= 1000", 20, "大额退款转人工审核"),
    ("refunding", "closed", "approve", "", 10, "退款审核通过"),
    ("refunding", "paid", "reject", "", 10, "退款驳回，回到待发货"),
    ("shipped", "completed", "confirm", "", 10, "确认收货"),
]

# ---------------- 商品 ----------------
PRODUCTS = [
    {
        "name": "iPhone 15 Pro",
        "category": "手机数码",
        "desc": "A17 Pro 芯片，钛金属机身",
        "skus": [("IP15-256-BLK", "256GB 黑色", "7999.00", 100), ("IP15-512-BLK", "512GB 黑色", "9999.00", 60)],
    },
    {
        "name": "MacBook Air 13",
        "category": "电脑办公",
        "desc": "M3 芯片，轻薄便携",
        "skus": [("MBA13-M3-8G", "M3 8+256", "7999.00", 50)],
    },
    {
        "name": "AirPods Pro 2",
        "category": "手机数码",
        "desc": "主动降噪，USB-C",
        "skus": [("APP2-USBC", "USB-C 版", "1899.00", 200)],
    },
    {
        "name": "小米 14",
        "category": "手机数码",
        "desc": "骁龙 8 Gen3",
        "skus": [("MI14-256", "12+256GB", "3999.00", 120)],
    },
    {
        "name": "机械键盘 K8",
        "category": "电脑办公",
        "desc": "客制化 Gasket 结构",
        "skus": [("K8-BROWN", "茶轴 87 键", "499.00", 300)],
    },
]


def seed_workflow(db) -> WorkflowDefinition:
    """创建订单流程定义（幂等）。"""
    existing = (
        db.execute(select(WorkflowDefinition).where(WorkflowDefinition.code == "order_flow"))
        .scalars()
        .first()
    )
    if existing:
        print("[seed] 流程 order_flow 已存在，跳过")
        return existing

    definition = WorkflowDefinition(
        code="order_flow", name="订单主流程", description="电商订单从下单到完成/关闭的标准流程",
        version=1, status="published",
    )
    db.add(definition)
    db.flush()

    db.add_all(
        [
            WorkflowNode(
                definition_id=definition.id, key=key, name=name,
                node_type=node_type, x=x, y=y,
            )
            for key, name, node_type, x, y in NODES
        ]
    )
    db.add_all(
        [
            WorkflowTransition(
                definition_id=definition.id, from_node_key=f, to_node_key=t,
                event=e, condition_expr=c, priority=p, description=d,
            )
            for f, t, e, c, p, d in TRANSITIONS
        ]
    )
    db.commit()
    print(f"[seed] 流程定义已创建：{len(NODES)} 节点 / {len(TRANSITIONS)} 流转")
    return definition


def seed_catalog(db) -> dict[str, int]:
    """创建分类、商品与 SKU（按 sku_code 幂等），返回 sku_code -> sku_id。"""
    code_to_id: dict[str, int] = {}
    for item in PRODUCTS:
        category = (
            db.execute(select(Category).where(Category.name == item["category"]))
            .scalars()
            .first()
        )
        if category is None:
            category = Category(name=item["category"])
            db.add(category)
            db.flush()

        product = (
            db.execute(select(Product).where(Product.name == item["name"])).scalars().first()
        )
        if product is None:
            product = Product(
                category_id=category.id, name=item["name"],
                description=item["desc"], status="on_sale",
            )
            db.add(product)
            db.flush()

        for sku_code, spec, price, stock in item["skus"]:
            sku = db.execute(select(Sku).where(Sku.sku_code == sku_code)).scalars().first()
            if sku is None:
                sku = Sku(
                    product_id=product.id, sku_code=sku_code, spec=spec,
                    price=Decimal(price), stock=stock, status="on_sale",
                )
                db.add(sku)
                db.flush()
            code_to_id[sku_code] = sku.id
    db.commit()
    print(f"[seed] 商品目录就绪：{len(code_to_id)} 个 SKU")
    return code_to_id


def seed_users(db) -> tuple[int, int, int]:
    """创建演示用户与收货地址。"""
    users = {}
    for username, nickname, phone in [("zhangsan", "张三", "13800000001"), ("lisi", "李四", "13800000002")]:
        user = db.execute(select(User).where(User.username == username)).scalars().first()
        if user is None:
            user = User(
                username=username, nickname=nickname, phone=phone,
                password_hash=hash_password("123456"),
            )
            db.add(user)
            db.flush()
        # zhangsan 为演示管理员（RBAC），其余为普通买家
        user.is_admin = username == "zhangsan"
        users[username] = user.id

    addr_id = None
    addr = db.execute(select(Address).where(Address.user_id == users["zhangsan"])).scalars().first()
    if addr is None:
        addr = Address(
            user_id=users["zhangsan"], receiver="张三", phone="13800000001",
            province="广东省", city="深圳市", district="南山区",
            detail="科技园南路 88 号 A 座 1201", is_default=True,
        )
        db.add(addr)
        db.flush()
    addr_id = addr.id
    db.commit()
    print(f"[seed] 用户就绪：{len(users)} 人")
    return users["zhangsan"], users["lisi"], addr_id


def clear_business_data(db) -> None:
    """清空订单与流程运行时数据，保留商品与用户。"""
    db.query(WorkflowTransitionLog).delete()
    db.query(WorkflowInstance).delete()
    db.query(Payment).delete()
    db.query(OrderItem).delete()
    db.query(Order).delete()
    db.commit()
    print("[seed] 已清空历史订单与流程实例")


def seed_orders(db, sku_map: dict[str, int], u1: int, u2: int, addr_id: int) -> None:
    """创建覆盖各状态的演示订单。"""
    svc = OrderService(db)

    def mk(user_id, sku_code, qty, addr=True):
        return {"user_id": user_id, "items": [{"sku_id": sku_map[sku_code], "quantity": qty}],
                "address_id": addr_id if addr else None}

    plans = [
        ("已完成", mk(u1, "APP2-USBC", 1), ["pay", "ship", "confirm"]),
        ("待发货", mk(u1, "K8-BROWN", 2), ["pay"]),
        ("已发货", mk(u2, "IP15-256-BLK", 1), ["pay", "ship"]),
        ("已关闭(取消)", mk(u2, "MI14-256", 1), ["cancel"]),
        ("退款审核中", mk(u1, "MBA13-M3-8G", 1), ["pay", "refund"]),  # 7999 触发大额分支
        ("待付款", mk(u1, "K8-BROWN", 1), []),
        ("已关闭(小额退款)", mk(u2, "K8-BROWN", 1), ["pay", "refund"]),  # 499 触发小额自动关单
    ]

    for label, payload, events in plans:
        order = svc.create_order(**payload)
        for ev in events:
            if ev == "pay":
                svc.pay(order.id)
            elif ev == "ship":
                svc.ship(order.id)
            elif ev == "confirm":
                svc.confirm(order.id)
            elif ev == "cancel":
                svc.cancel(order.id)
            elif ev == "refund":
                svc.refund(order.id)
        print(f"[seed] 订单 {order.order_no}  金额 {order.pay_amount}  → {label}")


def main() -> None:
    parser = argparse.ArgumentParser(description="灌入 flowmart 演示数据")
    parser.add_argument("--reset", action="store_true", help="先清空订单与流程数据再重建")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        seed_workflow(db)
        sku_map = seed_catalog(db)
        u1, u2, addr_id = seed_users(db)

        if args.reset:
            clear_business_data(db)
        existing_orders = db.execute(select(Order)).scalars().all()
        if existing_orders and not args.reset:
            print(f"[seed] 已存在 {len(existing_orders)} 个订单，跳过（如需重建加 --reset）")
        else:
            seed_orders(db, sku_map, u1, u2, addr_id)

        # 汇总
        print("\n[seed] ---- 数据概览 ----")
        for model, name in [
            (User, "用户"), (Product, "商品"), (Sku, "SKU"), (Order, "订单"),
            (WorkflowDefinition, "流程定义"), (WorkflowInstance, "流程实例"),
            (WorkflowTransitionLog, "流转日志"),
        ]:
            print(f"  {name:8s} {len(db.execute(select(model)).scalars().all())}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
