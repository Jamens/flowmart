"""Alembic 环境配置。

两个关键取舍：

1. **连接串不写在 alembic.ini**，而是取自 `app.core.config.settings`。
   `DB_DIALECT` / `.env` 已经是项目里数据库地址的唯一事实来源，再在 ini 里
   写一份必然会出现两处漂移。需要临时迁移别的库时用 `-x url=...` 覆盖。

2. **SQLite 必须开 `render_as_batch`**。SQLite 不支持大多数 ALTER（改列类型/约束），
   不开 batch 的话 autogenerate 生成的 `op.alter_column` 在 SQLite 上会直接报错；
   batch 模式会退化为「新建表 → 拷数据 → 删旧表 → 改名」。
"""
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 无论从哪个目录执行 alembic，都要能 import 到 backend/ 下的 app 包
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import settings  # noqa: E402
from app.core.database import Base  # noqa: E402
from app.models import ecommerce, workflow  # noqa: F401,E402  导入即注册表

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# autogenerate 依赖它来对比「模型 vs 库」
target_metadata = Base.metadata


def _resolve_url() -> str:
    """优先级：命令行 -x url=... > ini 的 sqlalchemy.url > 项目配置。"""
    x_args = context.get_x_argument(as_dictionary=True)
    url = x_args.get("url") or config.get_main_option("sqlalchemy.url") or settings.database_url
    return url


URL = _resolve_url()
# 回填给 config，让下面的 engine_from_config 能读到
config.set_main_option("sqlalchemy.url", URL)

IS_SQLITE = URL.startswith("sqlite")

if IS_SQLITE:
    # SQLite 文件路径的父目录不存在时，连接会报 "unable to open database file"，
    # 这里提前建好，避免把配置问题伪装成迁移失败
    path_part = URL.split("sqlite:///", 1)[-1]
    if path_part and path_part != ":memory:":
        Path(path_part).parent.mkdir(parents=True, exist_ok=True)


def run_migrations_offline() -> None:
    """离线模式：不连库，把 SQL 打印到 stdout（用于人工审核/DBA 执行）。"""
    context.configure(
        url=URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=IS_SQLITE,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # 列类型变化也要能被 autogenerate 发现（默认只比列名/可空性）
            compare_type=True,
            render_as_batch=IS_SQLITE,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
