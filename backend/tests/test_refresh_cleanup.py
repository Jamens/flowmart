"""refresh_tokens 定期清理测试。

核心不变量：**已轮换（used_at 非空）但尚未过期的行必须保留**——重放检测依赖它。
提前删掉会让重放退化成「查无此记录」分支：虽然同样是 401，却拿不到
「撤销整条 family」这一更强的处置。
"""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.security import hash_password, utcnow_naive
from app.models.ecommerce import RefreshToken, User
from scripts.cleanup_refresh_tokens import cleanup_refresh_tokens, main


def _user(db, username):
    u = User(
        username=username,
        email=f"{username}@example.com",
        email_verified=True,
        password_hash=hash_password("secret1"),
        is_active=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _row(db, user_id, jti, family, expires_at, used_at=None, revoked_at=None):
    rt = RefreshToken(
        user_id=user_id,
        jti=jti,
        family_id=family,
        expires_at=expires_at,
        used_at=used_at,
        revoked_at=revoked_at,
    )
    db.add(rt)
    return rt


def _remaining_jtis(db):
    return {r.jti for r in db.execute(select(RefreshToken)).scalars()}


def test_cleanup_keeps_rotated_tokens_until_expiry(db):
    u = _user(db, "cleaner")
    now = utcnow_naive()
    future = now + timedelta(days=5)

    _row(db, u.id, "expired", "f1", now - timedelta(days=1))            # 过期 -> 删
    _row(db, u.id, "old_revoked", "f2", future,                          # 撤销超保留期 -> 删
         revoked_at=now - timedelta(days=40))
    _row(db, u.id, "rotated", "f3", future,                              # 已轮换未过期 -> 必须留
         used_at=now - timedelta(hours=1))
    _row(db, u.id, "fresh", "f4", future)                                # 在用 -> 留
    _row(db, u.id, "recent_revoked", "f5", future,                       # 撤销未超期 -> 留
         revoked_at=now - timedelta(days=3))
    db.commit()

    res = cleanup_refresh_tokens(db, now=now, revoked_retention_days=30)
    assert res["deleted"] == 2
    assert _remaining_jtis(db) == {"rotated", "fresh", "recent_revoked"}


def test_dry_run_deletes_nothing(db):
    u = _user(db, "dryrun")
    now = utcnow_naive()
    _row(db, u.id, "expired2", "f6", now - timedelta(days=1))
    db.commit()

    res = cleanup_refresh_tokens(db, now=now, dry_run=True)
    assert res["deleted"] == 0 and res["expired"] == 1
    assert _remaining_jtis(db) == {"expired2"}


def test_row_matching_both_conditions_is_deleted_once(db):
    """既过期、又撤销超期的行：两个条件都命中，但并集去重后只删一次。"""
    u = _user(db, "overlap")
    now = utcnow_naive()
    _row(db, u.id, "both", "f8", now - timedelta(days=2), revoked_at=now - timedelta(days=40))
    db.commit()

    res = cleanup_refresh_tokens(db, now=now, revoked_retention_days=30)
    assert res["expired"] == 1 and res["revoked_stale"] == 1
    assert res["deleted"] == 1
    assert _remaining_jtis(db) == set()


def test_main_dry_run_keeps_rows(monkeypatch, db, tmp_path):
    """CLI 入口（参数解析 + 建引擎 + 退出码）也要覆盖：dry-run 不能真删。"""
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    now = utcnow_naive()
    _row(db, 1, "cli_expired", "f9", now - timedelta(days=1))
    db.commit()

    assert main(["--dry-run"]) == 0
    assert _remaining_jtis(db) == {"cli_expired"}


def test_main_rejects_negative_retention(monkeypatch, db, tmp_path):
    monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    with pytest.raises(SystemExit):
        main(["--revoked-retention-days", "-1"])


def test_revoked_retention_days_is_respected(db):
    """保留期调小后，撤销不久的记录也应被清掉。"""
    u = _user(db, "retention")
    now = utcnow_naive()
    _row(db, u.id, "rev3d", "f7", now + timedelta(days=5), revoked_at=now - timedelta(days=3))
    db.commit()

    res = cleanup_refresh_tokens(db, now=now, revoked_retention_days=1)
    assert res["deleted"] == 1
    assert _remaining_jtis(db) == set()
