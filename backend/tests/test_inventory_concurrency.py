"""库存并发控制测试。

验证 create_order 的扣减与 _effect_return_stock 的归还都是 DB 层原子操作，
在并发下不会超卖、不会丢失更新（lost update）。

实现要点：直接开多个线程、各自持独立 Session 连同一份文件 SQLite 库，
靠数据库自身的写锁把「原子 UPDATE」串行化，复现真实并发而不是 sleep 模拟。
"""
import threading

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.security import hash_password
from app.models.ecommerce import Order, Product, Sku, User
from app.models.workflow import WorkflowDefinition, WorkflowNode, WorkflowTransition
from app.services.order_service import OrderService


def _make_engine(tmp_path):
    url = f"sqlite:///{tmp_path / 'concurrency.db'}"
    eng = create_engine(
        url,
        future=True,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(eng)
    return eng


def _seed(eng, stocks):
    """建一个买家 + 一条 order_flow + 若干 SKU（stock 由 stocks 列表给定）。

    返回 (buyer_id, [sku_id, ...])。
    """
    Session = sessionmaker(bind=eng, future=True)
    with Session() as s:
        u = User(
            username="buyer_c",
            nickname="并发买家",
            password_hash=hash_password("123456"),
            is_admin=False,
        )
        s.add(u)
        s.flush()

        definition = WorkflowDefinition(
            code="order_flow", name="订单主流程", status="published", version=1
        )
        s.add(definition)
        s.flush()
        nodes = [
            WorkflowNode(definition_id=definition.id, key="start", name="开始", node_type="start"),
            WorkflowNode(definition_id=definition.id, key="pending_payment", name="待付款", node_type="task"),
            WorkflowNode(definition_id=definition.id, key="paid", name="待发货", node_type="task"),
            WorkflowNode(definition_id=definition.id, key="shipped", name="已发货", node_type="task"),
            WorkflowNode(definition_id=definition.id, key="completed", name="已完成", node_type="end"),
            WorkflowNode(definition_id=definition.id, key="closed", name="已关闭", node_type="end"),
        ]
        s.add_all(nodes)
        s.add_all([
            WorkflowTransition(definition_id=definition.id, from_node_key="start", to_node_key="pending_payment", event="submit", priority=10),
            WorkflowTransition(definition_id=definition.id, from_node_key="pending_payment", to_node_key="paid", event="pay", priority=10),
            WorkflowTransition(definition_id=definition.id, from_node_key="pending_payment", to_node_key="closed", event="cancel", priority=10),
            WorkflowTransition(definition_id=definition.id, from_node_key="paid", to_node_key="shipped", event="ship", priority=10),
            WorkflowTransition(definition_id=definition.id, from_node_key="paid", to_node_key="closed", event="refund", condition_expr="amount < 1000", priority=10),
            WorkflowTransition(definition_id=definition.id, from_node_key="shipped", to_node_key="completed", event="confirm", priority=10),
        ])

        sku_ids = []
        for i, stock in enumerate(stocks):
            p = Product(name=f"并发商品{i}", status="on_sale")
            s.add(p)
            s.flush()
            sku = Sku(
                product_id=p.id, sku_code=f"C-{i:03d}", spec="默认",
                price=100, stock=stock,
            )
            s.add(sku)
            s.flush()
            sku_ids.append(sku.id)

        s.commit()
        return u.id, sku_ids


def _fresh_stock(eng, sku_id):
    Session = sessionmaker(bind=eng, future=True)
    with Session() as s:
        return s.get(Sku, sku_id).stock


# ---------------- 单线程：功能正确性 ----------------

def test_atomic_deduction_reduces_stock_exactly(tmp_path):
    """买 3，库存 10 → 7，且按 DB 真实值核对（不是内存自减）。"""
    eng = _make_engine(tmp_path)
    buyer_id, (sku_id,) = _seed(eng, [10])
    Session = sessionmaker(bind=eng, future=True)
    with Session() as s:
        OrderService(s).create_order(
            user_id=buyer_id, items=[{"sku_id": sku_id, "quantity": 3}]
        )
    assert _fresh_stock(eng, sku_id) == 7


def test_insufficient_stock_rolls_back_clean(tmp_path):
    """买 999（库存 10）应抛库存不足，且不残留订单、库存不变。"""
    eng = _make_engine(tmp_path)
    buyer_id, (sku_id,) = _seed(eng, [10])
    Session = sessionmaker(bind=eng, future=True)
    with Session() as s:
        try:
            OrderService(s).create_order(
                user_id=buyer_id, items=[{"sku_id": sku_id, "quantity": 999}]
            )
            raise AssertionError("应当抛出库存不足")
        except ValueError as exc:
            assert "库存不足" in str(exc)
    with Session() as s:
        assert s.get(Sku, sku_id).stock == 10
        assert s.execute(select(Order)).scalars().all() == []


def test_multi_item_partial_failure_rolls_back_everything(tmp_path):
    """A 扣成功、B 库存不足时，整单回滚：A、B 库存都不变，无订单。

    重点验证「原子 UPDATE 失败分支」确实回滚了前面 SKU 已执行的扣减。
    """
    eng = _make_engine(tmp_path)
    buyer_id, (sku_a, sku_b) = _seed(eng, [5, 5])
    Session = sessionmaker(bind=eng, future=True)
    with Session() as s:
        try:
            OrderService(s).create_order(
                user_id=buyer_id,
                items=[
                    {"sku_id": sku_a, "quantity": 5},
                    {"sku_id": sku_b, "quantity": 999},
                ],
            )
            raise AssertionError("应当抛出库存不足")
        except ValueError:
            pass
    with Session() as s:
        assert s.get(Sku, sku_a).stock == 5
        assert s.get(Sku, sku_b).stock == 5
        assert s.execute(select(Order)).scalars().all() == []


def test_return_stock_restores_on_cancel(tmp_path):
    """下单买 4（10→6），取消后归还（6→10）。"""
    eng = _make_engine(tmp_path)
    buyer_id, (sku_id,) = _seed(eng, [10])
    Session = sessionmaker(bind=eng, future=True)
    with Session() as s:
        order = OrderService(s).create_order(
            user_id=buyer_id, items=[{"sku_id": sku_id, "quantity": 4}]
        )
        order_id = order.id
    assert _fresh_stock(eng, sku_id) == 6
    with Session() as s:
        OrderService(s).cancel(order_id)
    assert _fresh_stock(eng, sku_id) == 10


def test_duplicate_sku_in_one_order_rejected(tmp_path):
    """同一 SKU 在一单里出现两行各买 2（共需 4），库存仅 2：整单必须被拒、库存不变。

    这最考验「预检用内存库存 + 原子 UPDATE 用 DB 库存」两份真相的配合：
    无论第二行预检读到的是过期内存值还是刷新值，最终都靠原子 UPDATE 的
    rowcount==0 拦下，绝不会超卖。
    """
    eng = _make_engine(tmp_path)
    buyer_id, (sku_id,) = _seed(eng, [2])
    Session = sessionmaker(bind=eng, future=True)
    with Session() as s:
        try:
            OrderService(s).create_order(
                user_id=buyer_id,
                items=[
                    {"sku_id": sku_id, "quantity": 2},
                    {"sku_id": sku_id, "quantity": 2},
                ],
            )
            raise AssertionError("重复 SKU 超量应当被拒")
        except ValueError:
            pass
    with Session() as s:
        assert s.get(Sku, sku_id).stock == 2, "库存不能因半截扣减而减少"
        assert s.execute(select(Order)).scalars().all() == []


# ---------------- 多线程：并发安全 ----------------

def test_concurrent_orders_no_oversell(tmp_path):
    """库存=1，两笔同时下单各买 1：恰好一单成功、库存归零，绝不超卖。"""
    eng = _make_engine(tmp_path)
    buyer_id, (sku_id,) = _seed(eng, [1])

    results = {}
    barrier = threading.Barrier(2)

    def worker(tid):
        barrier.wait()
        Session = sessionmaker(bind=eng, future=True)
        s = Session()
        try:
            OrderService(s).create_order(
                user_id=buyer_id, items=[{"sku_id": sku_id, "quantity": 1}]
            )
            results[tid] = "ok"
        except ValueError:
            results[tid] = "fail"
        finally:
            s.close()

    t1 = threading.Thread(target=worker, args=(0,))
    t2 = threading.Thread(target=worker, args=(1,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    ok = [v for v in results.values() if v == "ok"]
    assert len(ok) == 1, f"期望恰好一单成功，实际: {results}"
    assert _fresh_stock(eng, sku_id) == 0, "库存必须归零，不能出现 1（超卖）"


def test_concurrent_return_no_lost_update(tmp_path):
    """同一 SKU 两笔订单各买 1（10→8），并发取消归还：库存必须精确回到 10。

    对照旧实现 sku.stock += qty（读-改-写）：两线程都读到 8、都写回 9，
    丢失一次加成 → 终值 9（错误）。原子 UPDATE ... SET stock=stock+qty 无此问题。
    """
    eng = _make_engine(tmp_path)
    buyer_id, (sku_id,) = _seed(eng, [10])
    Session = sessionmaker(bind=eng, future=True)

    with Session() as s:
        o1 = OrderService(s).create_order(
            user_id=buyer_id, items=[{"sku_id": sku_id, "quantity": 1}]
        )
        o1_id = o1.id
    with Session() as s:
        o2 = OrderService(s).create_order(
            user_id=buyer_id, items=[{"sku_id": sku_id, "quantity": 1}]
        )
        o2_id = o2.id
    assert _fresh_stock(eng, sku_id) == 8

    results = {}
    barrier = threading.Barrier(2)

    def cancel_worker(oid, tid):
        barrier.wait()
        Session = sessionmaker(bind=eng, future=True)
        s = Session()
        try:
            OrderService(s).cancel(oid)
            results[tid] = "ok"
        except Exception:
            results[tid] = "fail"
        finally:
            s.close()

    t1 = threading.Thread(target=cancel_worker, args=(o1_id, 0))
    t2 = threading.Thread(target=cancel_worker, args=(o2_id, 1))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert all(v == "ok" for v in results.values()), f"两笔取消都应成功: {results}"
    assert _fresh_stock(eng, sku_id) == 10, "归还不能丢失更新，必须精确回到 10"


def test_concurrent_cancel_same_order_returns_stock_once(tmp_path):
    """同一订单被并发取消两次：只能成功一次，库存只归还一次（8 -> 10，绝不能是 12）。

    这是工作流 CAS 修复的核心回归：
    - 修复前：两个请求都读到 pending_payment、都判定可流转、都执行归还库存
      -> 库存凭空 +2（8 -> 12）
    - 修复后：后到者条件更新认领失败，而副作用已挪到 fire 成功之后
      -> 失败路径上副作用根本没跑，库存精确回到 10

    注意与「顺序重复取消」区分：顺序第二次取消会在 available_events 处短路，
    根本走不到 fire()，因此**测不出**这个并发缺陷，必须用真并发。
    """
    eng = _make_engine(tmp_path)
    buyer_id, (sku_id,) = _seed(eng, [10])
    Session = sessionmaker(bind=eng, future=True)

    with Session() as s:
        order = OrderService(s).create_order(
            user_id=buyer_id, items=[{"sku_id": sku_id, "quantity": 2}]
        )
        oid = order.id
    assert _fresh_stock(eng, sku_id) == 8

    results = {}
    barrier = threading.Barrier(2)

    def worker(tid):
        barrier.wait()
        s = Session()
        try:
            OrderService(s).cancel(oid)
            results[tid] = "ok"
        except Exception:
            results[tid] = "fail"
        finally:
            s.rollback()
            s.close()

    t1 = threading.Thread(target=worker, args=(0,))
    t2 = threading.Thread(target=worker, args=(1,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    ok = [v for v in results.values() if v == "ok"]
    assert len(ok) == 1, f"并发取消同一订单应恰好一次成功，实际: {results}"
    assert _fresh_stock(eng, sku_id) == 10, "库存只能归还一次，不能变成 12"
