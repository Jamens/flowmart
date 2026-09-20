"""工作流域数据模型：可配置流程引擎的存储层。

核心思路：流程的「结构」与「运行」彻底分离。
- Definition/Node/Transition 描述流程长什么样（可由设计器编辑）
- Instance/TransitionLog 记录某笔业务跑到了哪一步（运行时数据）

这样改流程定义不会影响正在跑的实例，已跑的每一步也都有据可查。
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class WorkflowDefinition(Base):
    """流程定义。同一个 code 可有多版本，但同一时刻只能有一个 published 生效。"""

    __tablename__ = "wf_definitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # draft=草稿, published=已发布, archived=已归档
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    nodes: Mapped[list["WorkflowNode"]] = relationship(
        back_populates="definition", cascade="all, delete-orphan"
    )
    transitions: Mapped[list["WorkflowTransition"]] = relationship(
        back_populates="definition", cascade="all, delete-orphan"
    )


class WorkflowNode(Base):
    """流程节点。x/y 为设计器画布坐标，仅用于前端渲染，不参与引擎逻辑。"""

    __tablename__ = "wf_nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    definition_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("wf_definitions.id"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # start=开始, task=任务节点, end=结束
    node_type: Mapped[str] = mapped_column(String(20), nullable=False, default="task")
    x: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    y: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    meta: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    definition: Mapped["WorkflowDefinition"] = relationship(back_populates="nodes")


class WorkflowTransition(Base):
    """流转边：从 from_node_key 触发 event 后可走到 to_node_key。

    condition_expr 是可选的条件表达式（如 `amount > 1000`），
    用 simpleeval 安全求值，绝不用 eval —— 表达式来自用户输入，eval 等于开后门。
    priority 越小越优先匹配，用于实现「满足条件 A 走这条路，否则走那条」。
    """

    __tablename__ = "wf_transitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    definition_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("wf_definitions.id"), nullable=False, index=True
    )
    from_node_key: Mapped[str] = mapped_column(String(64), nullable=False)
    to_node_key: Mapped[str] = mapped_column(String(64), nullable=False)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    condition_expr: Mapped[str] = mapped_column(Text, nullable=False, default="")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    definition: Mapped["WorkflowDefinition"] = relationship(back_populates="transitions")


class WorkflowInstance(Base):
    """流程实例：一笔业务（如一张订单）对应一个实例。"""

    __tablename__ = "wf_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    definition_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("wf_definitions.id"), nullable=False, index=True
    )
    # 业务类型与业务 ID，如 order / 123，用于反查
    biz_type: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    biz_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    current_node_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # running=进行中, finished=已结束, canceled=已取消
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    context: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)


class WorkflowTransitionLog(Base):
    """流转日志：每一次状态推进都留痕，这是审计与排查问题的唯一依据。"""

    __tablename__ = "wf_transition_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("wf_instances.id"), nullable=False, index=True
    )
    from_node_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    to_node_key: Mapped[str] = mapped_column(String(64), nullable=False)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    operator: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    comment: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # 流转时的上下文快照：事后复盘能知道当时是依据什么数据做的判断
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
