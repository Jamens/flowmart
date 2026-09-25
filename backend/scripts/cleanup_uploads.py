"""清理孤儿上传文件（供 cron / 计划任务定期执行）。

为什么需要：商品被删除、或者改了封面之后，**旧图文件并不会消失**——
`/uploads` 只是磁盘目录，删数据库记录不会连带删文件。放任下去它会单调增长，
而这些文件还是**对外公开可读**的：既是成本问题，也是暴露面问题。

为什么用「扫描 + 保留期」而不是在业务代码里顺手删文件：
  1. 封面 URL 可能被多处引用（当前只有 products.cover，但将来可能有别的），
     在业务代码里删文件容易误删仍被引用的图；
  2. 刚上传的文件**还没挂到任何商品上**——上传与保存表单是两次请求。
     不留保留期的话，GC 会把用户刚传好、还没保存的封面直接删掉。

因此本脚本只删「未被任何 Product.cover 引用 **且** 超过保留期」的文件。

用法（建议每天跑一次）：
    python scripts/cleanup_uploads.py                    # 实际清理
    python scripts/cleanup_uploads.py --dry-run          # 只报告将删多少
    python scripts/cleanup_uploads.py --retention-hours 48
"""
import argparse
import os
import sys
import time
from pathlib import Path

# 让脚本可以直接运行：把 backend/ 加入模块搜索路径
BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.models.ecommerce import Product  # noqa: E402


def referenced_names(session) -> set[str]:
    """当前仍被引用的文件名集合（从 products.cover 的 URL 里取最后一段）。"""
    names = set()
    for (cover,) in session.execute(select(Product.cover)):
        if cover:
            names.add(cover.rsplit("/", 1)[-1])
    return names


def cleanup_uploads(
    session,
    now: float | None = None,
    retention_seconds: int | None = None,
    dry_run: bool = False,
) -> dict:
    """删除「未被引用的过期文件」，返回统计。

    now / retention_seconds 可注入，便于测试用固定时间断言。
    """
    if now is None:
        now = time.time()
    if retention_seconds is None:
        retention_seconds = settings.UPLOAD_ORPHAN_RETENTION_SECONDS
    if retention_seconds < 0:
        raise ValueError("retention_seconds 不能为负")

    empty = {
        "scanned": 0, "referenced": 0, "deleted": 0,
        "kept_recent": 0, "bytes_freed": 0, "dry_run": dry_run,
    }
    upload_dir = Path(settings.UPLOAD_DIR)
    if not upload_dir.exists():
        return empty

    referenced = referenced_names(session)
    scanned = deleted = kept_recent = bytes_freed = 0

    with os.scandir(upload_dir) as it:
        entries = list(it)

    for entry in entries:
        if not entry.is_file():
            continue
        scanned += 1
        if entry.name in referenced:
            continue
        try:
            st = entry.stat()
        except OSError:
            continue  # 并发下文件可能已被删
        if now - st.st_mtime < retention_seconds:
            kept_recent += 1  # 可能是刚上传、还没保存的图
            continue
        bytes_freed += st.st_size
        deleted += 1
        if not dry_run:
            try:
                Path(entry.path).unlink()
            except OSError:
                deleted -= 1
                bytes_freed -= st.st_size

    return {
        "scanned": scanned,
        "referenced": len(referenced),
        "deleted": deleted,
        "kept_recent": kept_recent,
        "bytes_freed": bytes_freed,
        "dry_run": dry_run,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="清理未被引用的孤儿上传文件")
    parser.add_argument("--dry-run", action="store_true", help="只报告将删除多少，不实际删除")
    parser.add_argument(
        "--retention-hours", type=float, default=None,
        help="孤儿文件保留小时数（默认取 UPLOAD_ORPHAN_RETENTION_SECONDS，即 24h）",
    )
    args = parser.parse_args()

    if args.retention_hours is not None and args.retention_hours < 0:
        parser.error("--retention-hours 不能为负")

    retention = (
        int(args.retention_hours * 3600)
        if args.retention_hours is not None
        else None
    )

    engine = create_engine(settings.DATABASE_URL, future=True)
    session = sessionmaker(bind=engine, future=True)()
    try:
        stats = cleanup_uploads(
            session, retention_seconds=retention, dry_run=args.dry_run
        )
    except ValueError as exc:
        print(f"[cleanup-uploads] 参数错误：{exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[cleanup-uploads] 执行失败：{exc}")
        return 1
    finally:
        session.close()
        engine.dispose()

    prefix = "[预演] " if stats["dry_run"] else ""
    print(
        f"{prefix}[cleanup-uploads] 扫描 {stats['scanned']} 个文件，"
        f"其中被引用 {stats['referenced']} 个；"
        f"删除孤儿 {stats['deleted']} 个（释放 "
        f"{stats['bytes_freed'] / 1024:.1f} KB），"
        f"保留期内跳过 {stats['kept_recent']} 个"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
