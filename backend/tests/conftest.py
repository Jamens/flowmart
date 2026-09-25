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
from app.core.security import get_current_user, get_optional_current_user, hash_password  # noqa: E402
from app.models import ecommerce, workflow  # noqa: F401,E402
from app.models.ecommerce import User  # noqa: E402
from app.services.workflow_engine import WorkflowEngine  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    """限流器是模块级单例，跨用例共享计数会互相干扰，必须每个用例重置。

    TestClient 的客户端地址恒为 "testclient"，所有用例共用同一个限流键——
    不重置的话，前面用例注册的账号数会累计到后面，导致**与限流无关**的用例
    莫名其妙拿到 429。登录限流原本由各用例自行 reset_all，这里统一收口。
    """
    from app.core.ratelimit import limiter, login_limiter

    login_limiter.reset_all()
    limiter.reset_all()
    yield
    login_limiter.reset_all()
    limiter.reset_all()


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
def current_user(db):
    """一个已注册（有密码）的默认登录用户，供接口测试充当「当前用户」。"""
    # 默认代表「已完成入驻、验证过联系方式的正常账号」，避免每个下单相关用例都重复置位。
    # 验证闸门（未验证禁止下单）的「未验证」分支由专门的闸门用例用临时未验证用户覆盖。
    u = User(username="tester", nickname="测试员", phone="13800000000",
             password_hash=hash_password("123456"), is_admin=True, email_verified=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture
def client(db, order_flow, current_user):
    """已「登录」的客户端：get_db 与 get_current_user 都被覆盖。

    - 接口看到的就是 current_user，无需在请求里塞 user_id；
    - client.user 是当前用户对象，client.as_user(other) 可临时切换身份，
      用来测试越权/隔离场景。
    """
    from fastapi.testclient import TestClient

    from app.core.database import get_db
    from app.main import app

    state = {"user": current_user}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: state["user"]
    # OTP 自验证接口改用 get_optional_current_user：已登录会话也必须被识别为「已登录」，
    # 否则依赖覆盖只认 get_current_user 时，这些接口会拿到 None 而误判为未登录。
    app.dependency_overrides[get_optional_current_user] = lambda: state["user"]
    with TestClient(app) as c:
        c.user = current_user
        c.as_user = lambda u: state.__setitem__("user", u)
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def raw_client(db, order_flow):
    """未覆盖鉴权的真实客户端：用来验证 401 / 带令牌 200 的真实鉴权链路。"""
    from fastapi.testclient import TestClient

    from app.core.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def buyer_user(db):
    """一个已注册的普通买家（非管理员），供测试买家侧行为。"""
    # 同 current_user：默认视为已验证的正常买家账号
    u = User(username="buyer", nickname="买家", phone="13800000003",
             password_hash=hash_password("123456"), is_admin=False, email_verified=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture
def buyer_client(db, order_flow, buyer_user):
    """已「登录」的非管理员客户端：在依赖注入（覆盖 get_current_user）路径下验证买家侧行为，
    补足 conftest 默认 current_user 为管理员导致的买家路径覆盖缺口（与 raw_client 真实鉴权路径互补）。"""
    from fastapi.testclient import TestClient

    from app.core.database import get_db
    from app.main import app

    state = {"user": buyer_user}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: state["user"]
    app.dependency_overrides[get_optional_current_user] = lambda: state["user"]
    with TestClient(app) as c:
        c.user = buyer_user
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
