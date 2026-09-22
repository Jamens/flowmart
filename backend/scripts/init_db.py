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
from app.core.security import hash_password, ensure_admin_exists  # noqa: E402
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
        # 存量库补丁：若初始管理员此前未验证，登录验证闸门会把它锁死，这里一并置为已验证
        if admin and not (admin.email_verified or admin.phone_verified):
            admin.email_verified = True
            s.commit()
            print(f"[init_db] 已将 {settings.BOOTSTRAP_ADMIN} 标记为已验证（避免被登录闸门锁死）")

        # 「零管理员」校验只在已经有用户时才成立：
        # 全新库上用户要等 seed 才创建，此时必然是 0 个用户，若照常校验会让
        # init_db 在全新安装时永远失败（而它本该只是建表）。
        # 真正的锁死风险是「有用户但没人管」，那是存量库才有的情况。
        has_any_user = s.execute(select(User.id).limit(1)).first() is not None
        if has_any_user:
            ensure_admin_exists(s)


def _schema_matches_models(engine) -> bool:
    """库结构是否与模型一致（**不依赖 alembic 版本历史**）。

    这里不能用 `alembic check`：它的语义是「当前版本之后还有没有待应用的迁移」，
    而一个还没打过版本标记的库会被当作 base，于是必然报告有差异 ——
    用它当「结构是否一致」的判据会永远误判（已实测踩过）。
    """
    try:
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext
    except ImportError:
        return True  # 无法比对时不阻拦，保持原有行为
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    return not diff


def _ensure_alembic_baseline(engine, url: str) -> None:
    """给 create_all 建出来的库打上「已迁移」标记。

    为什么必须做：create_all 不会写 alembic_version 记录，之后执行
    `alembic upgrade head` 会把这个库当成空库、重新去建表，直接报
    「table already exists」。打上当前版本的 baseline 后，两条路径就能共存：
    既保留 init_db 一键建库的开发便利，又不影响后续用 alembic 增量改结构。
    """
    insp = inspect(engine)
    if "alembic_version" in insp.get_table_names():
        # 关键：downgrade base 之后这张表还在、只是没有行了。
        # 只看「表是否存在」会误判成已被 alembic 接管而跳过标记，
        # 于是又掉回上面那个「表已存在」的坑 —— 所以必须看有没有版本记录。
        with engine.connect() as conn:
            if conn.execute(text("SELECT 1 FROM alembic_version LIMIT 1")).first():
                return  # 已有版本记录，交给 alembic 自己管，不要覆盖

    try:
        from alembic import command
        from alembic.config import Config
    except ImportError:
        print("[init_db] 未安装 alembic，跳过版本标记（后续 alembic 可能报表已存在）")
        return

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    # % 必须转义成 %%：configparser 会做插值，而 MySQL 密码特殊字符已被 percent-encode
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))

    # 只有结构与模型完全一致时打 baseline 才是安全的：
    # 否则会把「缺的列」也标记为已迁移，之后真正的迁移会被静默跳过。
    if not _schema_matches_models(engine):
        print(
            "[init_db] 警告：库结构与模型不一致，未打迁移标记；"
            "请先执行 `alembic upgrade head`（并确认模型改动已生成迁移）",
            file=sys.stderr,
        )
        return

    try:
        command.stamp(cfg, "head")
    except Exception as e:
        print(
            f"[init_db] 警告：打迁移标记失败（{e}）；表结构已就绪，但 alembic 未记录版本",
            file=sys.stderr,
        )
        return
    print("[init_db] 已标记当前数据库为最新迁移版本（alembic baseline）")


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
        # alembic_version 不在 Base.metadata 里，drop_all 不会删它，
        # 残留的旧版本号会让后续 stamp/upgrade 判断错乱，必须一起清掉
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
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
    try:
        _backfill_admin(engine)
    except RuntimeError as e:
        print(f"[init_db] 启动校验失败：{e}", file=sys.stderr)
        sys.exit(1)

    # 让 create_all 建出的库与 alembic 共存（否则后续 alembic upgrade 会报表已存在）
    _ensure_alembic_baseline(engine, url)

    tables = sorted(Base.metadata.tables.keys())
    print(f"[init_db] 建表完成，共 {len(tables)} 张表：")
    for name in tables:
        print(f"  - {name}")

    engine.dispose()


if __name__ == "__main__":
    main()
