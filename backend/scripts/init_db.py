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

from sqlalchemy import create_engine, inspect, select, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.database import Base  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models import ecommerce, workflow  # noqa: F401,E402  导入即注册表
from app.models.ecommerce import User  # noqa: E402


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


def _ensure_password_column(engine, dialect: str) -> None:
    """为已存在的 users 表补 password_hash 列（create_all 不会动存量表）。"""
    insp = inspect(engine)
    cols = [c["name"] for c in insp.get_columns("users")]
    if "password_hash" in cols:
        return
    with engine.begin() as conn:
        if dialect == "mysql":
            # NOT NULL 必须有默认值，否则存量行因无法填充而报错
            conn.execute(
                text("ALTER TABLE users ADD COLUMN password_hash VARCHAR(255) NOT NULL DEFAULT ''")
            )
        else:
            conn.execute(text("ALTER TABLE users ADD COLUMN password_hash VARCHAR(255) DEFAULT ''"))
    print("[init_db] 已为 users 表补充 password_hash 字段")


def _backfill_demo_passwords(engine) -> None:
    """开发便利：给无密码的用户设置演示密码 123456，避免存量账号无法登录。"""
    Session = sessionmaker(bind=engine)
    with Session() as s:
        empties = s.execute(select(User).where(User.password_hash == "")).scalars().all()
        if not empties:
            return
        for u in empties:
            u.password_hash = hash_password("123456")
        s.commit()
        print(f"[init_db] 为 {len(empties)} 个无密码用户设置演示密码（123456）")


def _ensure_admin_column(engine, dialect: str) -> None:
    """为已存在的 users 表补 is_admin 列（create_all 不会动存量表）。"""
    insp = inspect(engine)
    cols = [c["name"] for c in insp.get_columns("users")]
    if "is_admin" in cols:
        return
    with engine.begin() as conn:
        if dialect == "mysql":
            # TINYINT(1) 即布尔；NOT NULL 必须有默认值，否则存量行无法填充
            conn.execute(text("ALTER TABLE users ADD COLUMN is_admin TINYINT(1) NOT NULL DEFAULT 0"))
        else:
            conn.execute(text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0"))
    print("[init_db] 已为 users 表补充 is_admin 字段")


def _backfill_admin(engine) -> None:
    """开发便利：把配置的初始管理员（settings.BOOTSTRAP_ADMIN）标记为 is_admin，避免存量库无管理员可用。"""
    Session = sessionmaker(bind=engine)
    with Session() as s:
        admin = s.execute(select(User).where(User.username == settings.BOOTSTRAP_ADMIN)).scalars().first()
        if admin and not admin.is_admin:
            admin.is_admin = True
            s.commit()
            print(f"[init_db] 已将 {settings.BOOTSTRAP_ADMIN} 设为管理员")


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化 flowmart 数据库")
    parser.add_argument(
        "--dialect",
        choices=["mysql", "sqlite"],
        default=None,
        help="目标数据库类型（默认跟随配置 DB_DIALECT）",
    )
    parser.add_argument(
        "--sqlite-path",
        default="",
        help="覆盖 SQLite 文件路径（默认取配置 SQLITE_PATH 或项目根 flowmart.db）",
    )
    parser.add_argument(
        "--drop", action="store_true", help="先删除所有表再重建（会清空数据）"
    )
    args = parser.parse_args()

    # 未显式指定时跟随 settings.DB_DIALECT，保证配置是唯一事实来源
    dialect = args.dialect or settings.dialect

    if dialect == "sqlite":
        if args.sqlite_path:
            url = f"sqlite:///{Path(args.sqlite_path).as_posix()}"
        else:
            url = settings.database_url
        print(f"[init_db] SQLite: {url}")
    else:
        url = settings.database_url
        ensure_mysql_database(settings.server_database_url)

    engine = create_engine(
        url,
        future=True,
        connect_args={"check_same_thread": False} if dialect == "sqlite" else {},
    )

    if args.drop:
        Base.metadata.drop_all(engine)
        print("[init_db] 已删除所有表")

    Base.metadata.create_all(engine)

    # create_all 不会给已存在的表补列：对存量库（尤其是正在跑的 MySQL）补 password_hash。
    # 否则登录接口读 users.password_hash 会报「Unknown column」。
    _ensure_password_column(engine, dialect)

    # 同样补齐 RBAC 所需的 is_admin 列
    _ensure_admin_column(engine, dialect)

    # 开发便利：为没有密码的用户回填演示密码 123456，避免存量账号无法登录。
    # 生产环境应改成强制用户走「首次登录设置密码」，这里仅本地演示用。
    _backfill_demo_passwords(engine)

    # 开发便利：把配置的初始管理员（settings.BOOTSTRAP_ADMIN）标记为 is_admin，避免存量库无管理员可用。
    _backfill_admin(engine)

    tables = sorted(Base.metadata.tables.keys())
    print(f"[init_db] 建表完成，共 {len(tables)} 张表：")
    for name in tables:
        print(f"  - {name}")

    engine.dispose()


if __name__ == "__main__":
    main()
