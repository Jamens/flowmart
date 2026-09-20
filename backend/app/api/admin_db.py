"""SQLite 可视化管理 API。

安全边界（很重要，这类接口最容易出事）：
1. 表名必须来自数据库 inspector 的白名单，绝不能直接拼进 SQL —— 否则 URL 里的表名就是注入点。
2. query 接口只放行 SELECT / PRAGMA / EXPLAIN，写操作一律拒绝。
"""
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db

router = APIRouter(prefix="/admin/db", tags=["db-admin"])


def _q(name: str) -> str:
    """按方言引用标识符。

    SQLite 用双引号；MySQL 默认 ANSI_QUOTES 关闭时双引号表示字符串字面量，
    必须用反引号。不区分的话 `FROM "orders"` 在 MySQL 下会被当成常量而报错。
    """
    if settings.dialect == "mysql":
        return f"`{name}`"
    return f'"{name}"'

# 写操作黑名单：本地工具也不给删库的机会
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|truncate|attach|detach)\b",
    re.IGNORECASE,
)


class QueryIn(BaseModel):
    sql: str = Field(..., min_length=1, max_length=5000)
    limit: int = Field(200, ge=1, le=1000)


def _tables(insp) -> list[str]:
    return sorted(insp.get_table_names())


def _assert_table(insp, name: str) -> str:
    """校验表名合法，返回原表名。"""
    if name not in _tables(insp):
        raise HTTPException(status_code=404, detail=f"表 `{name}` 不存在")
    return name


@router.get("/tables", summary="列出所有表及行数")
def list_tables(db: Session = Depends(get_db)):
    insp = inspect(db.bind)
    result = []
    for name in _tables(insp):
        try:
            count = db.execute(text(f'SELECT COUNT(*) FROM {_q(name)}')).scalar()
        except Exception:  # noqa: BLE001
            count = -1
        cols = insp.get_columns(name)
        result.append(
            {
                "name": name,
                "rows": count,
                "columns": len(cols),
                "pk": [c["name"] for c in insp.get_pk_constraint(name).get("constrained_columns", [])],
            }
        )
    return {"tables": result}


@router.get("/tables/{name}", summary="表结构 + 数据预览")
def read_table(name: str, limit: int = 100, db: Session = Depends(get_db)):
    insp = inspect(db.bind)
    tbl = _assert_table(insp, name)
    limit = max(1, min(limit, 500))

    columns = [
        {
            "name": c["name"],
            "type": str(c["type"]),
            "nullable": bool(c.get("nullable", True)),
            "default": str(c.get("default")) if c.get("default") is not None else None,
            "pk": c["name"] in insp.get_pk_constraint(tbl).get("constrained_columns", []),
        }
        for c in insp.get_columns(tbl)
    ]

    fks = [
        {"column": fk["constrained_columns"][0], "ref": f"{fk['referred_table']}.{fk['referred_columns'][0]}"}
        for fk in insp.get_foreign_keys(tbl)
        if fk.get("constrained_columns") and fk.get("referred_columns")
    ]

    # limit 已按范围钳制且为 int，可安全拼接（SQL 参数不支持 LIMIT 占位符的方言差异）
    rows = db.execute(text(f'SELECT * FROM {_q(tbl)} LIMIT {limit}')).mappings().all()
    data = [{k: _serial(v) for k, v in row.items()} for row in rows]
    return {"name": tbl, "columns": columns, "foreign_keys": fks, "rows": data, "total_limit": limit}


@router.post("/query", summary="执行只读 SQL")
def run_query(payload: QueryIn, db: Session = Depends(get_db)):
    sql = payload.sql.strip().rstrip(";")
    if _FORBIDDEN.search(sql):
        raise HTTPException(status_code=400, detail="只允许 SELECT / PRAGMA / EXPLAIN 查询")

    try:
        rows = db.execute(text(sql)).mappings().all()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"SQL 执行失败：{exc}") from exc

    data = [{k: _serial(v) for k, v in r.items()} for r in rows][: payload.limit]
    return {"columns": list(data[0].keys()) if data else [], "rows": data, "count": len(data)}


def _serial(v):
    """把数据库类型转成 JSON 友好类型。"""
    from datetime import date, datetime
    from decimal import Decimal

    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        return f"<{len(v)} bytes>"
    return v
