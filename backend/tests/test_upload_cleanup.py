"""上传侧的生命周期：孤儿文件清理 + 配额。

清理为什么必须有**保留期**：上传与保存表单是两次请求，刚传好的图还没挂到
任何商品上，不留宽限期就会被 GC 当成孤儿删掉——用户会看到「刚传的封面没了」。

配额为什么要有**总量**这一层：单文件上限挡不住「慢慢攒满磁盘」。
"""
import os
import time
from pathlib import Path

import pytest

from app.core.config import settings
from app.models.ecommerce import Product
from scripts.cleanup_uploads import cleanup_uploads

# 结构完整的最小 PNG（与 test_upload.py 同一套约束）
_IHDR_DATA = (1).to_bytes(4, "big") + (1).to_bytes(4, "big") + bytes([8, 6, 0, 0, 0])
PNG = (
    b"\x89PNG\r\n\x1a\n"
    + (13).to_bytes(4, "big") + b"IHDR" + _IHDR_DATA + b"\x00\x00\x00\x00"
    + (0).to_bytes(4, "big") + b"IEND" + b"\xaeB\x60\x82"
)


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    """上传目录用 tmp_path 的**子目录**。

    坑：conftest 的 db 夹具会把 SQLite 的 test.db 建在 tmp_path 里。
    若直接把 UPLOAD_DIR 指向 tmp_path，清理脚本会把**数据库文件当成上传文件**
    一起扫进去（scanned 多 1、甚至可能误删）。真实部署同理——
    UPLOAD_DIR 绝不能和数据库/源码同目录，这也是 main.py 启动校验的理由。
    """
    d = tmp_path / "uploads"
    d.mkdir()
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(d))
    return d


def _mk_file(directory: Path, name: str, age_seconds: float) -> Path:
    """造一个文件并把 mtime 拨到 age_seconds 之前。"""
    f = directory / name
    f.write_bytes(b"x" * 100)
    old = time.time() - age_seconds
    os.utime(f, (old, old))
    return f


def _post(client):
    return client.post(
        "/api/v1/uploads", files={"file": ("a.png", PNG, "image/png")}
    )


# ---------------- 孤儿清理 ----------------


def test_referenced_file_is_kept(db, upload_dir):
    """仍在 products.cover 里的文件绝不能被删。"""
    kept = _mk_file(upload_dir, "kept.png", age_seconds=99999)
    db.add(Product(name="有封面的商品", cover=f"/uploads/{kept.name}", status="on_sale"))
    db.commit()

    stats = cleanup_uploads(db, retention_seconds=3600)
    assert stats["deleted"] == 0, stats
    assert kept.exists()


def test_old_orphan_is_deleted(db, upload_dir):
    """超过保留期且无人引用的文件应被清理。"""
    orphan = _mk_file(upload_dir, "orphan.png", age_seconds=99999)

    stats = cleanup_uploads(db, retention_seconds=3600)
    assert stats["deleted"] == 1, stats
    assert stats["bytes_freed"] == 100
    assert not orphan.exists()


def test_fresh_orphan_is_kept(db, upload_dir):
    """刚上传、还没保存的图不能被删——这正是保留期存在的理由。"""
    fresh = _mk_file(upload_dir, "fresh.png", age_seconds=10)

    stats = cleanup_uploads(db, retention_seconds=3600)
    assert stats["deleted"] == 0, stats
    assert stats["kept_recent"] == 1, stats
    assert fresh.exists()


def test_dry_run_does_not_delete(db, upload_dir):
    orphan = _mk_file(upload_dir, "o.png", age_seconds=99999)

    stats = cleanup_uploads(db, retention_seconds=3600, dry_run=True)
    assert stats["deleted"] == 1, "预演应报告将删 1 个"
    assert orphan.exists(), "预演不能真的删"


def test_negative_retention_rejected(db, upload_dir):
    with pytest.raises(ValueError):
        cleanup_uploads(db, retention_seconds=-1)


def test_missing_upload_dir_is_noop(db, tmp_path, monkeypatch):
    """目录不存在时不应报错（还没人上传过）。"""
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "nope"))
    stats = cleanup_uploads(db, retention_seconds=3600)
    assert stats["scanned"] == 0


# ---------------- 配额 ----------------


def test_upload_rejected_when_total_quota_exceeded(client, upload_dir, monkeypatch):
    """总量上限：单文件再小，攒满了也得拒，而不是写到一半失败。"""
    monkeypatch.setattr(settings, "UPLOAD_TOTAL_MAX_BYTES", 10)
    (upload_dir / "existing.png").write_bytes(b"x" * 20)

    r = _post(client)
    assert r.status_code == 507
    assert "空间" in r.json()["detail"]


def test_upload_within_total_quota_succeeds(client, upload_dir, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_TOTAL_MAX_BYTES", 10 * 1024 * 1024)
    assert _post(client).status_code == 201


def test_upload_over_rate_limit_returns_429(client, upload_dir, monkeypatch):
    """刷上传是最直接的磁盘 DoS，按 user_id 限。"""
    monkeypatch.setattr(settings, "RATE_LIMIT_UPLOAD_MAX", 1)
    assert _post(client).status_code == 201
    assert _post(client).status_code == 429
