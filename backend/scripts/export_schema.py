"""导出数据库表结构为 Markdown，用于查阅与评审。

用法：
    python scripts/export_schema.py > ../docs/database-schema.md
"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.core.database import Base  # noqa: E402
from app.models import ecommerce, workflow  # noqa: F401,E402

# 表 -> 业务域分组，让文档读起来有结构而不是一堆平铺的表格
DOMAIN_MAP = {
    "users": "电商域",
    "addresses": "电商域",
    "categories": "电商域",
    "products": "电商域",
    "skus": "电商域",
    "cart_items": "电商域",
    "orders": "电商域",
    "order_items": "电商域",
    "payments": "电商域",
    "wf_definitions": "工作流域",
    "wf_nodes": "工作流域",
    "wf_transitions": "工作流域",
    "wf_instances": "工作流域",
    "wf_transition_logs": "工作流域",
}


def render() -> str:
    lines = ["# flowmart 数据库表结构", ""]
    lines.append("> 由 `backend/scripts/export_schema.py` 自动生成，请勿手工编辑。")
    lines.append("")

    for domain in ("电商域", "工作流域"):
        lines.append(f"## {domain}")
        lines.append("")
        for table_name, table in Base.metadata.tables.items():
            if DOMAIN_MAP.get(table_name) != domain:
                continue
            comment = table.comment or ""
            lines.append(f"### `{table_name}`" + (f" — {comment}" if comment else ""))
            lines.append("")
            lines.append("| 字段 | 类型 | 允许空 | 主键 | 说明 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for col in table.columns:
                pk = "✅" if col.primary_key else ""
                nullable = "是" if col.nullable else "否"
                lines.append(
                    f"| `{col.name}` | {col.type} | {nullable} | {pk} | |"
                )
            lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    print(render())
