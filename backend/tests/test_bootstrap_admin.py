"""初始管理员（BOOTSTRAP_ADMIN）配置化验证。

原本 zhangsan 被硬编码为管理员；改为从 settings.BOOTSTRAP_ADMIN 读取后，
必须保证：① seed 跟随配置而非写死 zhangsan；② 配置为空时启动即报错（避免静默锁死）。
"""
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import Settings, settings  # noqa: E402
from app.models.ecommerce import User  # noqa: E402
from scripts.seed import seed_users  # noqa: E402


def test_bootstrap_admin_follows_config(db):
    """把初始管理员改成一个非 zhangsan 的账号，seed 应当把它设为管理员、zhangsan 保持买家。"""
    original = settings.BOOTSTRAP_ADMIN
    settings.BOOTSTRAP_ADMIN = "lisi"
    try:
        seed_users(db)
        zhangsan = db.execute(select(User).where(User.username == "zhangsan")).scalars().first()
        lisi = db.execute(select(User).where(User.username == "lisi")).scalars().first()
        assert zhangsan is not None and lisi is not None
        assert zhangsan.is_admin is False
        assert lisi.is_admin is True
    finally:
        settings.BOOTSTRAP_ADMIN = original


def test_empty_bootstrap_admin_rejected(monkeypatch):
    """BOOTSTRAP_ADMIN 为空 = 谁都不提升为管理员，系统会静默锁死；必须在启动时即报错。"""
    monkeypatch.setenv("DEBUG", "True")
    monkeypatch.setenv("BOOTSTRAP_ADMIN", "")
    with pytest.raises(ValueError, match="BOOTSTRAP_ADMIN"):
        Settings()


def test_padded_bootstrap_admin_is_normalized(monkeypatch):
    """带首尾空格的用户名（如 " zhangsan "）应在启动时归一化为 "zhangsan"，避免匹配不上导致静默锁死。"""
    monkeypatch.setenv("DEBUG", "True")
    monkeypatch.setenv("BOOTSTRAP_ADMIN", " zhangsan ")
    assert Settings().BOOTSTRAP_ADMIN == "zhangsan"
