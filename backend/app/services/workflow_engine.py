"""可配置工作流引擎。

引擎只做一件事：给定当前节点，根据「事件 + 条件」决定下一步去哪，并留痕。

设计原则：
1. 流程规则只来自数据库，代码里没有任何 if 状态判断 —— 改流程不用改代码。
2. 条件表达式用 simpleeval 求值而非 eval。表达式可由用户在设计器里填写，
   用 eval 等于把服务器权限交出去，这是硬性安全红线。
3. 引擎只 flush 不 commit，把事务边界交给调用方。这样「扣库存 + 推进流程」
   能在同一个事务里完成，避免库存扣了但流程没走的中间态。
"""
from datetime import datetime
from typing import Any

from simpleeval import EvalWithCompoundTypes, simple_eval
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.workflow import (
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowNode,
    WorkflowTransition,
    WorkflowTransitionLog,
)


class WorkflowError(Exception):
    """工作流相关业务异常：定义缺失、非法流转、无可用路径等。"""


def eval_condition(expr: str, context: dict[str, Any]) -> bool:
    """求值条件表达式。空表达式视为无条件通过。

    使用 simpleeval：它只支持字面量与运算符，不暴露内置函数与属性访问，
    能挡住 `().__class__.__bases__[0].__subclasses__()` 这类沙箱逃逸写法。
    """
    if not expr or not expr.strip():
        return True
    try:
        result = simple_eval(expr, functions={}, names=dict(context))
        return bool(result)
    except Exception as exc:  # noqa: BLE001  表达式写错时按「不满足」处理并记日志
        raise WorkflowError(f"条件表达式求值失败 `{expr}`: {exc}") from exc


class WorkflowEngine:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ---------- 定义查询 ----------

    def get_published_definition(self, code: str) -> WorkflowDefinition:
        """取已发布的流程定义。同一 code 只应有一个 published 版本。"""
        stmt = (
            select(WorkflowDefinition)
            .where(WorkflowDefinition.code == code, WorkflowDefinition.status == "published")
            .order_by(WorkflowDefinition.version.desc())
        )
        definition = self.db.execute(stmt).scalars().first()
        if definition is None:
            raise WorkflowError(f"流程 `{code}` 没有已发布的版本")
        return definition

    def get_node(self, definition_id: int, key: str) -> WorkflowNode:
        stmt = select(WorkflowNode).where(
            WorkflowNode.definition_id == definition_id, WorkflowNode.key == key
        )
        node = self.db.execute(stmt).scalars().first()
        if node is None:
            raise WorkflowError(f"流程定义 {definition_id} 中不存在节点 `{key}`")
        return node

    # ---------- 实例生命周期 ----------

    def start(
        self,
        code: str,
        biz_type: str,
        biz_id: str,
        context: dict[str, Any] | None = None,
        operator: str = "system",
    ) -> WorkflowInstance:
        """启动一个流程实例，初始落在 start 节点。"""
        definition = self.get_published_definition(code)
        stmt = select(WorkflowNode).where(
            WorkflowNode.definition_id == definition.id, WorkflowNode.node_type == "start"
        )
        start_node = self.db.execute(stmt).scalars().first()
        if start_node is None:
            raise WorkflowError(f"流程 `{code}` 缺少 start 节点")

        instance = WorkflowInstance(
            definition_id=definition.id,
            biz_type=biz_type,
            biz_id=biz_id,
            current_node_key=start_node.key,
            status="running",
            context=dict(context or {}),
        )
        self.db.add(instance)
        self.db.flush()

        self._write_log(
            instance=instance,
            from_key="",
            to_key=start_node.key,
            event="start",
            operator=operator,
            comment="流程启动",
            snapshot=instance.context,
        )
        return instance

    def _pick(self, candidates: list[WorkflowTransition], ctx: dict[str, Any]):
        """按顺序选出第一条「条件成立」的流转边。

        求值失败的边会被跳过 —— 这与 available_events 的行为保持一致。
        否则会出现「前端显示可执行、点击却必然报错」的矛盾：
        UI 跳过了那条坏边给出按钮，而 fire 却在坏边上直接抛异常。

        但如果所有候选边都是因为表达式写错而失败，则统一抛出，
        避免配置错误被彻底静默吞掉。
        """
        errors: list[str] = []
        for trans in candidates:
            try:
                if eval_condition(trans.condition_expr, ctx):
                    return trans
            except WorkflowError as exc:
                errors.append(str(exc))
        if errors:
            raise WorkflowError("流转条件求值失败：" + "；".join(errors))
        return None

    def available_events(
        self, instance_id: int, runtime_context: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """列出当前节点上「条件已满足」的可触发事件。

        前端据此渲染按钮：能触发的才显示，避免用户点了才报错。
        """
        instance = self._get_instance(instance_id)
        # 与 fire() 口径一致：已结束的实例没有任何可执行事件。
        # 否则前端会渲染出「点了必然报错」的按钮——fire() 里的状态校验会直接抛错。
        if instance.status != "running":
            return []
        ctx = {**(instance.context or {}), **(runtime_context or {})}

        stmt = (
            select(WorkflowTransition)
            .where(
                WorkflowTransition.definition_id == instance.definition_id,
                WorkflowTransition.from_node_key == instance.current_node_key,
            )
            # 同级 priority 时按 id 兜底，否则路由结果不确定
            .order_by(WorkflowTransition.priority, WorkflowTransition.id)
        )
        result = []
        for trans in self.db.execute(stmt).scalars().all():
            # 求值失败视为不可用，但不阻断其它路径
            try:
                if not eval_condition(trans.condition_expr, ctx):
                    continue
            except WorkflowError:
                continue
            result.append(
                {
                    "event": trans.event,
                    "to_node_key": trans.to_node_key,
                    "description": trans.description,
                    "condition_expr": trans.condition_expr,
                }
            )
        return result

    def fire(
        self,
        instance_id: int,
        event: str,
        runtime_context: dict[str, Any] | None = None,
        operator: str = "system",
        comment: str = "",
    ) -> WorkflowInstance:
        """触发事件，推进流程到下一个节点。

        多条候选边按 priority 升序匹配，取第一条「条件成立」的。
        一条都不成立时抛错 —— 静默忽略会让业务以为操作成功了。
        """
        instance = self._get_instance(instance_id)
        if instance.status != "running":
            raise WorkflowError(f"流程实例 {instance_id} 已结束，不能再流转")

        ctx = {**(instance.context or {}), **(runtime_context or {})}

        stmt = (
            select(WorkflowTransition)
            .where(
                WorkflowTransition.definition_id == instance.definition_id,
                WorkflowTransition.from_node_key == instance.current_node_key,
                WorkflowTransition.event == event,
            )
            .order_by(WorkflowTransition.priority, WorkflowTransition.id)
        )
        candidates = self.db.execute(stmt).scalars().all()
        if not candidates:
            raise WorkflowError(
                f"当前节点 `{instance.current_node_key}` 不支持事件 `{event}`"
            )

        chosen = self._pick(candidates, ctx)
        if chosen is None:
            raise WorkflowError(
                f"事件 `{event}` 在当前上下文下没有满足条件的流转路径"
            )

        from_key = instance.current_node_key
        # 用「条件 UPDATE + rowcount」认领这次推进，而不是裸赋值。
        # 并发下两个请求会各自读到同一个 current_node_key、都判定「可流转」，
        # 裸赋值时后提交者直接覆盖前者 —— cancel/refund 的副作用（归还库存）
        # 就会执行两次，库存凭空变多。条件里带上 from_key 与 status，
        # 让后到的请求必然匹配不到行，从而显式失败而不是静默覆盖。
        res = self.db.execute(
            update(WorkflowInstance)
            .where(
                WorkflowInstance.id == instance.id,
                WorkflowInstance.current_node_key == from_key,
                WorkflowInstance.status == "running",
            )
            .values(current_node_key=chosen.to_node_key, context=ctx),
            # 不让 SQLAlchemy 同步内存对象：成败一律由下面 rowcount + 回读判定。
            # 失败路径尤其不能把「目标值」写进内存，否则调用方 catch 后继续用会读到假状态。
            execution_options={"synchronize_session": False},
        )
        if res.rowcount != 1:
            # rowcount 在 MySQL 下是「变更行数」而非「匹配行数」：
            # 自环流转（from == to）且 context 未变时，匹配到 1 行但没改动 -> rowcount=0。
            # SQLite 返回匹配数（1），所以这个差异测试跑不出来、只有生产 MySQL 会暴露。
            # 因此回读一次，区分「没人抢先、只是没变更」与「真的被并发推进了」。
            row = self.db.execute(
                select(WorkflowInstance.current_node_key, WorkflowInstance.status)
                .where(WorkflowInstance.id == instance.id)
            ).first()
            if row is None or row[0] != from_key or row[1] != "running":
                self.db.expire(instance)  # 清掉内存态，避免调用方 catch 后读到被改错的对象
                raise WorkflowError(
                    f"流程实例 {instance_id} 已被并发操作推进"
                    f"（当前节点不再是 `{from_key}`），请刷新后重试"
                )
            # 走到这里：行仍在原节点且运行中 —— 是我们自己匹配到了、只是值没变（自环），视为成功
        # 让 ORM 对象反映 DB 当前状态：后续结束节点判定与写日志都基于新值
        # （引擎只 flush 不 commit，同一事务内读得到自己刚写的行）
        self.db.refresh(instance)

        self._write_log(
            instance=instance,
            from_key=from_key,
            to_key=chosen.to_node_key,
            event=event,
            operator=operator,
            comment=comment,
            snapshot=ctx,
        )

        # 到达结束节点自动收敛实例
        to_node = self.get_node(instance.definition_id, chosen.to_node_key)
        if to_node.node_type == "end":
            instance.status = "finished"
            instance.finished_at = datetime.now()
            self.db.flush()

        return instance

    # ---------- 内部辅助 ----------

    def _get_instance(self, instance_id: int) -> WorkflowInstance:
        instance = self.db.get(WorkflowInstance, instance_id)
        if instance is None:
            raise WorkflowError(f"流程实例 {instance_id} 不存在")
        return instance

    def _write_log(
        self,
        instance: WorkflowInstance,
        from_key: str,
        to_key: str,
        event: str,
        operator: str,
        comment: str,
        snapshot: dict[str, Any],
    ) -> WorkflowTransitionLog:
        log = WorkflowTransitionLog(
            instance_id=instance.id,
            from_node_key=from_key,
            to_node_key=to_key,
            event=event,
            operator=operator,
            comment=comment,
            snapshot=snapshot,
        )
        self.db.add(log)
        self.db.flush()
        return log
