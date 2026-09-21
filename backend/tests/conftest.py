"""pytest 公共夹具。

测试一律跑在临时 SQLite 文件上：每个用例独立、互不污染，且不需要外部服务。
"""
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.database import Base  # noqa: E402
from app.models import ecommerce, workflow  # noqa: F401,E402
from app.services.workflow_engine import WorkflowEngine  # noqa: E402


@pytest.fixture
def db(tmp_path):
    """每个用例一份独立的 SQLite 库。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def engine(db):
    return WorkflowEngine(db)


@pytest.fixture
def client(db, order_flow):
    """把接口的数据库会话替换为测试库，并预置已发布的订单流程。

    order_flow 是必需的：下单类接口会去查 published 的 order_flow 定义。
    """
    from fastapi.testclient import TestClient

    from app.core.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def order_flow(db):
    """建一条订单流程定义：start → 待付款 → 待发货 → 已发货 → 已完成。

    同时带两条分支：待付款可取消到「已关闭」，待发货退款时按金额走不同终点，
    用来验证条件表达式确实生效。
    """
    from app.models.workflow import WorkflowDefinition, WorkflowNode, WorkflowTransition

    definition = WorkflowDefinition(
        code="order_flow", name="订单主流程", status="published", version=1
    )
    db.add(definition)
    db.flush()

    nodes = [
        WorkflowNode(definition_id=definition.id, key="start", name="开始", node_type="start"),
        WorkflowNode(definition_id=definition.id, key="pending_payment", name="待付款", node_type="task"),
        WorkflowNode(definition_id=definition.id, key="paid", name="待发货", node_type="task"),
        WorkflowNode(definition_id=definition.id, key="shipped", name="已发货", node_type="task"),
        WorkflowNode(definition_id=definition.id, key="completed", name="已完成", node_type="end"),
        WorkflowNode(definition_id=definition.id, key="closed", name="已关闭", node_type="end"),
    ]
    db.add_all(nodes)

    transitions = [
        # 下单：进入待付款
        WorkflowTransition(definition_id=definition.id, from_node_key="start",
                           to_node_key="pending_payment", event="submit", priority=10),
        # 支付成功
        WorkflowTransition(definition_id=definition.id, from_node_key="pending_payment",
                           to_node_key="paid", event="pay", priority=10),
        # 未支付取消
        WorkflowTransition(definition_id=definition.id, from_node_key="pending_payment",
                           to_node_key="closed", event="cancel", priority=10),
        # 发货
        WorkflowTransition(definition_id=definition.id, from_node_key="paid",
                           to_node_key="shipped", event="ship", priority=10),
        # 退款：小额直接关闭，大额需人工介入（走 closed 但条件不同）
        WorkflowTransition(definition_id=definition.id, from_node_key="paid",
                           to_node_key="closed", event="refund",
                           condition_expr="amount < 1000", priority=10,
                           description="小额退款自动关闭"),
        # 确认收货
        WorkflowTransition(definition_id=definition.id, from_node_key="shipped",
                           to_node_key="completed", event="confirm", priority=10),
    ]
    db.add_all(transitions)
    db.commit()
    return definition
