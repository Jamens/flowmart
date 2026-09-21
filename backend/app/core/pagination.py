"""列表分页公共工具。

设计约定：**limit 默认 0 表示不分页（返回全部）**。

为什么不默认分页：项目里已有调用方需要全量数据（例如「选择 SKU」的下拉框要列出
所有在售商品），一刀切地默认分页会把它们悄悄截断，属于隐蔽的功能退化。
因此只有显式传 limit 的调用方才真正分页。

返回结构统一为 {"items": [...], "total": N}：
- 即使不分页也带 total，前端不必区分两种响应形态；
- total 用子查询统计，避免把整表数据捞出来再数。
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session


def apply_pagination(stmt, limit: int = 0, offset: int = 0):
    """给查询加上 LIMIT / OFFSET。limit=0 或 offset=0 表示不加对应子句。"""
    if offset and offset > 0:
        stmt = stmt.offset(offset)
    if limit and limit > 0:
        stmt = stmt.limit(limit)
    return stmt


def total_count(db: Session, stmt) -> int:
    """统计查询的总行数（忽略已有的排序与加载选项，只看过滤条件）。"""
    return db.execute(select(func.count()).select_from(stmt.subquery())).scalar() or 0
