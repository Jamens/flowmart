"""工作流定义 API：前端流程设计器的读写后端。

保存策略：**节点与流转边全量替换**。
增量 diff 合并要处理「新增/修改/删除」三种情况且容易出错，而一次流程定义最多几十个节点，
全量替换的代价远低于逻辑复杂度。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user
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
def list_definitions(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    defs = db.execute(select(WorkflowDefinition).order_by(WorkflowDefinition.id)).scalars().all()
    return [
        {"id": d.id, "code": d.code, "name": d.name, "version": d.version, "status": d.status}
        for d in defs
    ]


@router.get("/definitions/{definition_id}", summary="流程定义图（设计器加载用）")
def get_definition(definition_id: int, db: Session = Depends(get_db)):
    definition = db.get(WorkflowDefinition, definition_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="流程定义不存在")
    return _graph(definition, db)


@router.get("/definitions/code/{code}", summary="按 code 取已发布流程")
def get_definition_by_code(code: str, db: Session = Depends(get_db)):
    definition = db.execute(
        select(WorkflowDefinition)
        .where(WorkflowDefinition.code == code, WorkflowDefinition.status == "published")
        .order_by(WorkflowDefinition.version.desc())
    ).scalars().first()
    if definition is None:
        raise HTTPException(status_code=404, detail=f"流程 `{code}` 没有已发布版本")
    return _graph(definition, db)


@router.post("/definitions", status_code=201, summary="创建流程定义")
def create_definition(payload: DefinitionIn, db: Session = Depends(get_db)):
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


@router.put("/definitions/{definition_id}", summary="更新流程定义（全量替换图）")
def update_definition(
    definition_id: int, payload: DefinitionIn, db: Session = Depends(get_db)
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


@router.post("/definitions/{definition_id}/publish", summary="发布流程定义")
def publish_definition(definition_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
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

    definition.status = "published"
    db.commit()
    return {"id": definition.id, "status": definition.status}


@router.delete("/definitions/{definition_id}", summary="归档流程定义")
def archive_definition(definition_id: int, db: Session = Depends(get_db)):
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
