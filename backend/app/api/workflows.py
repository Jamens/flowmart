"""工作流定义 API：前端流程设计器的读写后端。

保存策略：**节点与流转边全量替换**。
增量 diff 合并要处理「新增/修改/删除」三种情况且容易出错，而一次流程定义最多几十个节点，
全量替换的代价远低于逻辑复杂度。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user, require_admin
from app.models.ecommerce import User
from app.models.workflow import (
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowNode,
    WorkflowTransition,
)

router = APIRouter(prefix="/workflows", tags=["工作流"])


class NodeIn(BaseModel):
    key: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=128)
    node_type: str = Field("task", pattern="^(start|task|end)$")
    x: int = 0
    y: int = 0
    meta: dict = Field(default_factory=dict)


class TransitionIn(BaseModel):
    from_node_key: str
    to_node_key: str
    event: str = Field(..., min_length=1, max_length=64)
    condition_expr: str = ""
    priority: int = 100
    description: str = ""


class DefinitionIn(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=128)
    description: str = ""
    nodes: list[NodeIn] = Field(default_factory=list)
    transitions: list[TransitionIn] = Field(default_factory=list)


def _graph(definition: WorkflowDefinition, db: Session) -> dict:
    """把定义序列化成设计器可直接消费的图结构。"""
    nodes = db.execute(
        select(WorkflowNode).where(WorkflowNode.definition_id == definition.id)
    ).scalars().all()
    transitions = db.execute(
        select(WorkflowTransition)
        .where(WorkflowTransition.definition_id == definition.id)
        .order_by(WorkflowTransition.priority)
    ).scalars().all()
    return {
        "id": definition.id,
        "code": definition.code,
        "name": definition.name,
        "description": definition.description,
        "version": definition.version,
        "status": definition.status,
        "nodes": [
            {"key": n.key, "name": n.name, "node_type": n.node_type, "x": n.x, "y": n.y, "meta": n.meta}
            for n in nodes
        ],
        "transitions": [
            {
                "from": t.from_node_key,
                "to": t.to_node_key,
                "event": t.event,
                "condition_expr": t.condition_expr,
                "priority": t.priority,
                "description": t.description,
            }
            for t in transitions
        ],
    }


@router.get("/definitions", summary="流程定义列表")
def list_definitions(
    code: str | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """流程定义列表。?code= 可按业务 code 过滤（同一 code 会有多个版本行）。"""
    stmt = select(WorkflowDefinition)
    if code:
        stmt = stmt.where(WorkflowDefinition.code == code)
    defs = db.execute(stmt.order_by(WorkflowDefinition.id)).scalars().all()
    return [
        {"id": d.id, "code": d.code, "name": d.name, "version": d.version, "status": d.status}
        for d in defs
    ]


@router.get("/definitions/{definition_id}", summary="流程定义图（设计器加载用）")
def get_definition(
    definition_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    definition = db.get(WorkflowDefinition, definition_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="流程定义不存在")
    return _graph(definition, db)


@router.get("/definitions/code/{code}", summary="按 code 取已发布流程")
def get_definition_by_code(
    code: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    definition = db.execute(
        select(WorkflowDefinition)
        .where(WorkflowDefinition.code == code, WorkflowDefinition.status == "published")
        .order_by(WorkflowDefinition.version.desc())
    ).scalars().first()
    if definition is None:
        raise HTTPException(status_code=404, detail=f"流程 `{code}` 没有已发布版本")
    return _graph(definition, db)


@router.get("/definitions/code/{code}/versions", summary="流程版本历史")
def list_versions(
    code: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """同一 code 的所有版本（含 draft/published/archived），按 version 倒序。

    用于前端「版本列表」与「回滚」入口：回滚 = 把某个旧版本 publish（publish 会自动降级其它 published 版本）。
    """
    defs = db.execute(
        select(WorkflowDefinition)
        .where(WorkflowDefinition.code == code)
        .order_by(WorkflowDefinition.version.desc())
    ).scalars().all()
    if not defs:
        raise HTTPException(status_code=404, detail=f"流程 `{code}` 不存在")
    return [
        {
            "id": d.id,
            "version": d.version,
            "status": d.status,
            "name": d.name,
            "created_at": d.created_at,
            "updated_at": d.updated_at,
        }
        for d in defs
    ]


@router.post("/definitions", status_code=201, summary="创建流程定义（仅管理员）")
def create_definition(
    payload: DefinitionIn,
    # 流程定义就是订单状态机：谁能改图、谁能发布，谁就能决定订单往哪流转。
    # 只要求登录的话，任意买家都能改写并发布状态机，严重性高于商品下架。
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if db.execute(
        select(WorkflowDefinition).where(WorkflowDefinition.code == payload.code)
    ).scalars().first():
        raise HTTPException(status_code=400, detail=f"流程 code `{payload.code}` 已存在")

    definition = WorkflowDefinition(
        code=payload.code, name=payload.name, description=payload.description,
        version=1, status="draft",
    )
    db.add(definition)
    db.flush()
    _replace_graph(definition, payload, db)
    db.commit()
    return {"id": definition.id, "code": definition.code, "status": definition.status}


@router.put("/definitions/{definition_id}", summary="更新流程定义（全量替换图，仅管理员）")
def update_definition(
    definition_id: int,
    payload: DefinitionIn,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    definition = db.get(WorkflowDefinition, definition_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="流程定义不存在")
    if definition.status == "published":
        # 防止线上正在跑的流程被静默改动，必须先归档或另起版本
        raise HTTPException(status_code=400, detail="已发布的流程不可直接修改，请先归档")

    definition.name = payload.name
    definition.description = payload.description
    _replace_graph(definition, payload, db)
    db.commit()
    return {"id": definition.id, "status": definition.status}


@router.post("/definitions/{definition_id}/publish", summary="发布流程定义（仅管理员）")
def publish_definition(definition_id: int, current_user: User = Depends(require_admin), db: Session = Depends(get_db)):
    definition = db.get(WorkflowDefinition, definition_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="流程定义不存在")

    nodes = db.execute(
        select(WorkflowNode).where(WorkflowNode.definition_id == definition.id)
    ).scalars().all()
    errors = []
    if not any(n.node_type == "start" for n in nodes):
        errors.append("缺少 start 节点")
    if not any(n.node_type == "end" for n in nodes):
        errors.append("缺少 end 节点")
    keys = {n.key for n in nodes}
    transitions = db.execute(
        select(WorkflowTransition).where(WorkflowTransition.definition_id == definition.id)
    ).scalars().all()
    for t in transitions:
        if t.from_node_key not in keys or t.to_node_key not in keys:
            errors.append(f"流转 `{t.from_node_key}`→`{t.to_node_key}` 引用了不存在的节点")
    if errors:
        # 发布前校验：把问题挡在运行之前，而不是等订单流转时才炸
        raise HTTPException(status_code=400, detail={"message": "流程校验未通过", "errors": errors})

    # 保证全局唯一 published：把同 code 的其它已发布版本降级为 archived。
    # 在途实例按 definition_id 钉死在各自版本上，不受此影响。
    siblings = db.execute(
        select(WorkflowDefinition).where(
            WorkflowDefinition.code == definition.code,
            WorkflowDefinition.status == "published",
            WorkflowDefinition.id != definition.id,
        )
    ).scalars().all()
    for sib in siblings:
        sib.status = "archived"

    definition.status = "published"
    db.commit()
    return {"id": definition.id, "status": definition.status, "demoted": [s.id for s in siblings]}


@router.post("/definitions/{definition_id}/versions", status_code=201, summary="派生新版本（克隆图）")
def fork_version(
    definition_id: int, current_user: User = Depends(require_admin), db: Session = Depends(get_db)
):
    """从任意版本派生一个新草稿版本：克隆节点与流转边，version = max(同 code 版本) + 1。

    典型用途：
    - 在已发布流程上迭代（clone 后改图、再 publish，旧版本自动归档）
    - 回滚改造：clone 一个旧版本 → 改 → publish（等价于「基于旧版本出新版」）
    在途实例不受影响——它们钉死在各自的 definition_id 上。
    """
    source = db.get(WorkflowDefinition, definition_id)
    if source is None:
        raise HTTPException(status_code=404, detail="流程定义不存在")

    # 同一 code 下取最大版本号，避免重复（并发创建时取 DB 当前最大值，非内存计数）
    max_version = db.execute(
        select(func.max(WorkflowDefinition.version)).where(WorkflowDefinition.code == source.code)
    ).scalar()
    next_version = (max_version or 0) + 1

    new_def = WorkflowDefinition(
        code=source.code, name=source.name, description=source.description,
        version=next_version, status="draft",
    )
    db.add(new_def)
    db.flush()

    src_nodes = db.execute(
        select(WorkflowNode).where(WorkflowNode.definition_id == source.id)
    ).scalars().all()
    src_trans = db.execute(
        select(WorkflowTransition).where(WorkflowTransition.definition_id == source.id)
    ).scalars().all()
    db.add_all(
        [
            WorkflowNode(
                definition_id=new_def.id, key=n.key, name=n.name,
                node_type=n.node_type, x=n.x, y=n.y, meta=n.meta,
            )
            for n in src_nodes
        ]
    )
    db.add_all(
        [
            WorkflowTransition(
                definition_id=new_def.id, from_node_key=t.from_node_key,
                to_node_key=t.to_node_key, event=t.event, condition_expr=t.condition_expr,
                priority=t.priority, description=t.description,
            )
            for t in src_trans
        ]
    )
    db.commit()
    return {
        "id": new_def.id, "code": new_def.code, "version": new_def.version,
        "status": new_def.status, "source_id": source.id, "cloned_nodes": len(src_nodes),
    }


@router.delete("/definitions/{definition_id}", summary="归档流程定义（仅管理员）")
def archive_definition(
    definition_id: int,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    definition = db.get(WorkflowDefinition, definition_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="流程定义不存在")

    # 归档前必须确认没有在途实例：归档后引擎只查 published 版本，
    # 正在跑的订单会永久卡在当前节点且无法回退，属于不可逆事故
    running = (
        db.execute(
            select(WorkflowInstance).where(
                WorkflowInstance.definition_id == definition.id,
                WorkflowInstance.status == "running",
            )
        )
        .scalars()
        .all()
    )
    if running:
        raise HTTPException(
            status_code=400,
            detail=f"该流程仍有 {len(running)} 个进行中的实例，归档后它们将无法继续流转",
        )

    definition.status = "archived"
    db.commit()
    return {"id": definition.id, "status": definition.status}


def _replace_graph(definition: WorkflowDefinition, payload: DefinitionIn, db: Session) -> None:
    """全量替换节点与流转边。"""
    db.query(WorkflowTransition).filter(
        WorkflowTransition.definition_id == definition.id
    ).delete()
    db.query(WorkflowNode).filter(WorkflowNode.definition_id == definition.id).delete()
    db.flush()

    db.add_all(
        [
            WorkflowNode(
                definition_id=definition.id, key=n.key, name=n.name,
                node_type=n.node_type, x=n.x, y=n.y, meta=n.meta,
            )
            for n in payload.nodes
        ]
    )
    db.add_all(
        [
            WorkflowTransition(
                definition_id=definition.id, from_node_key=t.from_node_key,
                to_node_key=t.to_node_key, event=t.event, condition_expr=t.condition_expr,
                priority=t.priority, description=t.description,
            )
            for t in payload.transitions
        ]
    )
    db.flush()
