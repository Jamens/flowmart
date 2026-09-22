"""init_db._backfill_demo_passwords 生产安全闸门测试。

核心不变量：已知明文演示密码 123456 只能在 DEBUG（开发）环境回填；
生产环境（DEBUG=False）绝不能静默写入已知密码，否则空密码存量账户会被统一接管。
"""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.security import verify_password
from app.models.ecommerce import User
from scripts import init_db


class _FakeSettings:
    DEBUG = True


def _make_engine():
    eng = create_engine("sqlite://", future=True)
    Base.metadata.create_all(eng)
    return eng


def _add_empty_password_user(engine, username):
    Session = sessionmaker(bind=engine)
    with Session() as s:
        s.add(User(username=username, password_hash=""))
        s.commit()


def test_backfill_sets_demo_password_in_debug(monkeypatch):
    fake = _FakeSettings()
    fake.DEBUG = True
    monkeypatch.setattr(init_db, "settings", fake)
    eng = _make_engine()
    _add_empty_password_user(eng, "devuser")

    init_db._backfill_demo_passwords(eng)

    Session = sessionmaker(bind=eng)
    with Session() as s:
        u = s.execute(select(User).where(User.username == "devuser")).scalars().first()
        # hash_password 带随机盐，不能直接比 hash；用 verify_password 校验被置为 123456
        assert verify_password("123456", u.password_hash)


def test_backfill_skips_known_password_in_production(monkeypatch, capsys):
    fake = _FakeSettings()
    fake.DEBUG = False
    monkeypatch.setattr(init_db, "settings", fake)
    eng = _make_engine()
    _add_empty_password_user(eng, "produser")

    init_db._backfill_demo_passwords(eng)

    Session = sessionmaker(bind=eng)
    with Session() as s:
        u = s.execute(select(User).where(User.username == "produser")).scalars().first()
        # 生产环境绝不给已知明文密码，避免账号被接管
        assert u.password_hash == ""
    # 生产分支应打印告警（而非静默放行），证明走的是 DEBUG=False 分支
    assert "[警告]" in capsys.readouterr().out
