"""工作流引擎测试。

重点覆盖三类风险：
1. 正常流转是否按定义推进
2. 非法流转是否被拒绝（这是状态机最容易出错的地方）
3. 条件表达式是否真正参与路由决策
"""
import pytest

from app.models.workflow import WorkflowTransitionLog
from app.services.workflow_engine import WorkflowEngine, WorkflowError


def test_start_lands_on_start_node(engine, order_flow):
    """启动后应停在 start 节点，且状态为 running。"""
    inst = engine.start("order_flow", biz_type="order", biz_id="1", context={"amount": 500})
    assert inst.current_node_key == "start"
    assert inst.status == "running"
    assert inst.context == {"amount": 500}


def test_full_happy_path(engine, order_flow):
    """完整主流程：下单 → 支付 → 发货 → 确认收货。"""
    inst = engine.start("order_flow", biz_type="order", biz_id="1", context={"amount": 500})

    engine.fire(inst.id, "submit")
    assert inst.current_node_key == "pending_payment"

    engine.fire(inst.id, "pay")
    assert inst.current_node_key == "paid"

    engine.fire(inst.id, "ship")
    assert inst.current_node_key == "shipped"

    engine.fire(inst.id, "confirm")
    assert inst.current_node_key == "completed"
    # 到达 end 节点后实例应自动收敛
    assert inst.status == "finished"
    assert inst.finished_at is not None


def test_illegal_event_rejected(engine, order_flow):
    """在待付款节点触发「发货」必须报错，不能静默通过。"""
    inst = engine.start("order_flow", biz_type="order", biz_id="1")
    engine.fire(inst.id, "submit")

    with pytest.raises(WorkflowError, match="不支持事件"):
        engine.fire(inst.id, "ship")

    assert inst.current_node_key == "pending_payment"


def test_cancel_branch(engine, order_flow):
    """待付款可取消，直接走到已关闭。"""
    inst = engine.start("order_flow", biz_type="order", biz_id="2")
    engine.fire(inst.id, "submit")
    engine.fire(inst.id, "cancel")

    assert inst.current_node_key == "closed"
    assert inst.status == "finished"


def test_condition_decides_route(engine, order_flow):
    """同样触发 refund，金额不同应走向不同结果。

    小额（<1000）条件成立 → 关闭；大额条件不成立 → 无可用路径报错。
    """
    # 小额退款
    small = engine.start("order_flow", biz_type="order", biz_id="3", context={"amount": 500})
    engine.fire(small.id, "submit")
    engine.fire(small.id, "pay")
    engine.fire(small.id, "refund")
    assert small.current_node_key == "closed"

    # 大额退款：条件不成立，应拒绝
    big = engine.start("order_flow", biz_type="order", biz_id="4", context={"amount": 5000})
    engine.fire(big.id, "submit")
    engine.fire(big.id, "pay")
    with pytest.raises(WorkflowError, match="没有满足条件"):
        engine.fire(big.id, "refund")
    assert big.current_node_key == "paid"


def test_available_events_filters_by_condition(engine, order_flow):
    """available_events 只返回条件成立的事件，供前端渲染按钮。"""
    big = engine.start("order_flow", biz_type="order", biz_id="5", context={"amount": 5000})
    engine.fire(big.id, "submit")
    engine.fire(big.id, "pay")

    events = [e["event"] for e in engine.available_events(big.id)]
    assert "ship" in events
    # 大额时 refund 的条件不成立，不应出现
    assert "refund" not in events


def test_transition_logs_recorded(engine, order_flow):
    """每一步流转都要留痕，包含启动共 4 条日志。"""
    inst = engine.start("order_flow", biz_type="order", biz_id="6", context={"amount": 100})
    engine.fire(inst.id, "submit")
    engine.fire(inst.id, "pay")

    logs = (
        engine.db.query(WorkflowTransitionLog)
        .filter(WorkflowTransitionLog.instance_id == inst.id)
        .order_by(WorkflowTransitionLog.id)
        .all()
    )
    assert len(logs) == 3
    assert logs[0].event == "start"
    assert logs[1].event == "submit"
    assert logs[2].event == "pay"
    # 快照应记录当时的上下文，便于事后复盘
    assert logs[2].snapshot["amount"] == 100


def test_finished_instance_cannot_fire(engine, order_flow):
    """已结束的实例不允许再流转。"""
    inst = engine.start("order_flow", biz_type="order", biz_id="7")
    engine.fire(inst.id, "submit")
    engine.fire(inst.id, "cancel")

    with pytest.raises(WorkflowError, match="已结束"):
        engine.fire(inst.id, "pay")


def test_missing_definition_raises(engine):
    """流程未发布时启动应报错，避免默默创建一个没有规则的实例。"""
    with pytest.raises(WorkflowError, match="没有已发布"):
        engine.start("not_exist", biz_type="order", biz_id="8")


def test_malformed_expression_raises(engine, order_flow, db):
    """表达式语法错误必须显式报错，不能吞掉。"""
    from app.models.workflow import WorkflowTransition

    bad = WorkflowTransition(
        definition_id=order_flow.id,
        from_node_key="paid",
        to_node_key="closed",
        event="bad_event",
        condition_expr="amount >>>",
        priority=1,
    )
    db.add(bad)
    db.commit()

    inst = engine.start("order_flow", biz_type="order", biz_id="9", context={"amount": 10})
    engine.fire(inst.id, "submit")
    engine.fire(inst.id, "pay")

    with pytest.raises(WorkflowError, match="求值失败"):
        engine.fire(inst.id, "bad_event")
