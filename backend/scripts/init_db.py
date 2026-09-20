"""初始化数据库：建库（MySQL）+ 建表。

用法：
    python scripts/init_db.py                    # 使用 .env / 默认配置（MySQL）
    python scripts/init_db.py --dialect sqlite   # 生成本地 SQLite 文件，便于可视化查看
    python scripts/init_db.py --drop             # 先删再建（开发期重置，会丢数据！）
"""
import argparse
import sys
from pathlib import Path

# 让脚本可以直接运行：把 backend/ 加入模块搜索路径
BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.database import Base  # noqa: E402
from app.models import ecommerce, workflow  # noqa: F401,E402  导入即注册表


def ensure_mysql_database(url: str) -> None:
    """MySQL 需要先建库，SQLite 会自动创建文件。"""
    engine = create_engine(url, future=True)
    with engine.connect() as conn:
        conn.execute(
            text(
                f"CREATE DATABASE IF NOT EXISTS `{settings.DB_NAME}` "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci"
            )
        )
        conn.commit()
    engine.dispose()
    print(f"[init_db] 数据库 `{settings.DB_NAME}` 已就绪")


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化 flowmart 数据库")
    parser.add_argument(
        "--dialect",
        choices=["mysql", "sqlite"],
        default="mysql",
        help="目标数据库类型（默认 mysql）",
    )
    parser.add_argument(
        "--sqlite-path",
        default=str(Path(__file__).resolve().parents[2] / "flowmart.db"),
        help="SQLite 文件存放路径",
    )
    parser.add_argument(
        "--drop", action="store_true", help="先删除所有表再重建（会清空数据）"
    )
    args = parser.parse_args()

    if args.dialect == "sqlite":
        db_path = Path(args.sqlite_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{db_path.as_posix()}"
        print(f"[init_db] SQLite 文件: {db_path}")
    else:
        url = settings.database_url
        ensure_mysql_database(settings.server_database_url)

    engine = create_engine(
        url,
        future=True,
        connect_args={"check_same_thread": False} if args.dialect == "sqlite" else {},
    )

    if args.drop:
        Base.metadata.drop_all(engine)
        print("[init_db] 已删除所有表")

    Base.metadata.create_all(engine)

    tables = sorted(Base.metadata.tables.keys())
    print(f"[init_db] 建表完成，共 {len(tables)} 张表：")
    for name in tables:
        print(f"  - {name}")

    engine.dispose()


if __name__ == "__main__":
    main()
