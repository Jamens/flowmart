"""Alembic 迁移回归测试。

为什么需要这一层：只有「迁移能真正建出与模型一致的库」，迁移脚本才是有用的。
否则会出现最坑的情况 —— 迁移文件存在，但漏了某次模型改动，于是在全新环境部署后
缺表缺列，而开发环境因为库是历史遗留的、一直没暴露。

策略：在临时 SQLite 上真跑 `alembic upgrade head`，再用 `alembic check`
断言「库结构 == 模型」，任何模型改动未写迁移都会被这里拦下。
"""
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

BACKEND_DIR = Path(__file__).resolve().parents[1]

from app.core.database import Base  # noqa: E402
from app.models import ecommerce, workflow  # noqa: F401,E402  导入即注册表


def _run_alembic(url: str, *args: str) -> subprocess.CompletedProcess:
    """在 backend/ 下执行 alembic，并用 -x url 指向临时库。"""
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"url={url}", *args],
        cwd=str(BACKEND_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


@pytest.fixture
def mig_url(tmp_path):
    return f"sqlite:///{(tmp_path / 'mig.db').as_posix()}"


def test_upgrade_head_creates_every_table(mig_url):
    """全新库执行 upgrade head 后，模型里的表必须一张不少。"""
    r = _run_alembic(mig_url, "upgrade", "head")
    assert r.returncode == 0, f"upgrade 失败:\n{r.stdout}\n{r.stderr}"

    engine = create_engine(mig_url)
    try:
        existing = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    missing = set(Base.metadata.tables.keys()) - existing
    assert not missing, f"迁移后仍缺表：{sorted(missing)}"


def test_migrated_schema_matches_models(mig_url):
    """迁移后的库必须与模型完全一致 —— 这是「改模型忘了写迁移」的守门测试。"""
    assert _run_alembic(mig_url, "upgrade", "head").returncode == 0

    r = _run_alembic(mig_url, "check")
    # alembic check 在「无待迁移变更」时返回 0，有差异时返回非 0
    assert r.returncode == 0, (
        f"模型与迁移不一致（可能改了模型却没生成迁移）：\n{r.stdout}\n{r.stderr}"
    )
    assert "No new upgrade operations detected" in r.stdout


def test_downgrade_drops_tables(mig_url):
    """降级必须能真正拆掉表，保证迁移是双向可用的。"""
    assert _run_alembic(mig_url, "upgrade", "head").returncode == 0
    r = _run_alembic(mig_url, "downgrade", "base")
    assert r.returncode == 0, f"downgrade 失败:\n{r.stdout}\n{r.stderr}"

    engine = create_engine(mig_url)
    try:
        remaining = [
            t for t in inspect(engine).get_table_names() if t != "alembic_version"
        ]
    finally:
        engine.dispose()
    assert remaining == [], f"降级后仍有残留表：{remaining}"
